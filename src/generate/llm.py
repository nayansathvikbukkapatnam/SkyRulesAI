"""One LLM interface for the generator and the LLM judge, with two backends.

    llm = LLMClient(load_config("b2"))
    result = llm.complete(system, messages, max_tokens=None)

LLM_PROVIDER (in .env, or llm_provider in a config) picks the backend:
- anthropic: the Claude API (Messages API, anthropic SDK). Reads ANTHROPIC_API_KEY
  and ANTHROPIC_MODEL_ID.
- bedrock: AWS Bedrock (Converse API, boto3). Reads AWS_PROFILE, AWS_REGION and
  BEDROCK_MODEL_ID.

Both return the same LLMResult. Responses are cached on disk in .cache/llm/, keyed
by the SHA-256 of the request, so re-running an eval does not pay twice. Rate
limits, overloaded errors, 5xx errors and timeouts are retried here with
exponential backoff (the SDKs' own retries are off, so there is one policy).

Request settings come from `generation` in configs/default.yaml:
- temperature: null by default and then never sent (Claude Haiku 5.5 returns a 400
  for any temperature other than 1). An explicit value is sent, and if the API
  rejects it the client drops it, logs a warning once and records None.
- thinking ("disabled" | "adaptive") and effort (low ... max, or null): sent by the
  Anthropic backend as `thinking` and `output_config.effort`. "disabled" with effort
  xhigh or max is a config error, raised before any call (the API would return 400).

A reply cut off at max_tokens is marked truncated and never yields parsed JSON.

Credentials never pass through this module: the SDKs read them from the
environment or the AWS profile, and nothing here logs or caches them.

Smoke test (one live call, no cache):
    python -m src.generate.llm --smoke
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from src.common import paths
from src.common.config import MODEL_ID_ENV, env, load_config

log = logging.getLogger(__name__)

ROLES = ("user", "assistant")
THINKING_MODES = ("disabled", "adaptive")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Thinking "disabled" is accepted only at effort high or below (thinking troubleshooting docs).
EFFORTS_NOT_ALLOWED_WITHOUT_THINKING = ("xhigh", "max")


class LLMError(RuntimeError):
    """The LLM call failed after retries. Callers fail closed (refuse) on this."""


class LLMConfigError(ValueError):
    """The generation settings in config are invalid. Raised before any API call."""


def validate_generation(generation: dict[str, Any]) -> None:
    thinking, effort = generation.get("thinking"), generation.get("effort")
    if thinking not in THINKING_MODES:
        raise LLMConfigError(
            f"generation.thinking must be one of {THINKING_MODES}, got {thinking!r}"
        )
    if effort is not None and effort not in EFFORTS:
        raise LLMConfigError(f"generation.effort must be one of {EFFORTS} or null, got {effort!r}")
    if thinking == "disabled" and effort in EFFORTS_NOT_ALLOWED_WITHOUT_THINKING:
        raise LLMConfigError(
            f"generation.thinking 'disabled' cannot be combined with effort {effort!r}; "
            "use effort 'high' or below, or set thinking to 'adaptive'"
        )


@dataclass
class Reply:
    """What a backend returns for one successful call."""

    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None


@dataclass
class LLMResult:
    text: str
    parsed_json: Any  # the reply as a dict or list if it is JSON, else None
    input_tokens: int
    output_tokens: int
    latency_ms: int  # wall time of the original call, retries included; kept on a cache hit
    cost_usd: float | None  # None when no price applies (see configs/default.yaml)
    provider: str
    model_id: str
    stop_reason: str | None
    truncated: bool  # stop_reason == "max_tokens": the reply was cut off
    temperature: float | None  # None when not set in config, or rejected and dropped
    thinking: str | None  # thinking mode sent; None when the backend does not send it
    effort: str | None  # effort sent; None when unset or the backend does not send it
    cached: bool


class Backend(Protocol):
    name: str
    sends_thinking: bool  # whether send() passes thinking and effort to the API

    def send(
        self,
        model_id: str,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float | None,
        thinking: str | None,
        effort: str | None,
    ) -> Reply: ...

    def is_retryable(self, exc: Exception) -> bool: ...

    def rejects_temperature(self, exc: Exception) -> bool: ...


# --- Anthropic (Claude API) --------------------------------------------------


class AnthropicBackend:
    name = "anthropic"
    sends_thinking = True

    def __init__(self, client: Any = None, timeout_s: float = 60):
        if client is None:
            import anthropic

            # The SDK reads ANTHROPIC_API_KEY from the environment (.env is loaded by
            # src.common.config). Its own retries are off: LLMClient retries.
            client = anthropic.Anthropic(max_retries=0, timeout=timeout_s)
        self.client = client

    def send(self, model_id, system, messages, max_tokens, temperature, thinking, effort) -> Reply:
        params: dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if thinking is not None:
            params["thinking"] = {"type": thinking}
        if effort is not None:
            params["output_config"] = {"effort": effort}
        if temperature is not None:
            # anthropic 1.x removed `temperature` from create(); the API decides whether the
            # model accepts it, so it goes in the request body as-is.
            params["extra_body"] = {"temperature": temperature}
        response = self.client.messages.create(**params)
        # Only text blocks: thinking blocks (when thinking is on) are skipped.
        text = "".join(b.text for b in response.content if b.type == "text")
        return Reply(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
        )

    def is_retryable(self, exc: Exception) -> bool:
        import anthropic

        if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
            return True
        if isinstance(exc, anthropic.APIStatusError):
            # 429 rate limit, 529 overloaded, other 5xx server errors, 408 timeout
            return exc.status_code in (408, 429) or exc.status_code >= 500
        return False

    def rejects_temperature(self, exc: Exception) -> bool:
        import anthropic

        return isinstance(exc, anthropic.BadRequestError) and "temperature" in str(exc).lower()


# --- AWS Bedrock (Converse API) ----------------------------------------------

BEDROCK_RETRY_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelNotReadyException",
    "ModelTimeoutException",
}


class BedrockBackend:
    name = "bedrock"
    sends_thinking = False  # thinking and effort are not wired for Converse yet

    def __init__(self, client: Any = None, timeout_s: float = 60):
        if client is None:
            import boto3
            from botocore.config import Config

            session = boto3.Session(
                profile_name=env("AWS_PROFILE", required=True),
                region_name=env("AWS_REGION", required=True),
            )
            # botocore's own retries are off: LLMClient retries.
            client = session.client(
                "bedrock-runtime",
                config=Config(read_timeout=timeout_s, retries={"total_max_attempts": 1}),
            )
        self.client = client

    def send(self, model_id, system, messages, max_tokens, temperature, thinking, effort) -> Reply:
        inference: dict[str, Any] = {"maxTokens": max_tokens}
        if temperature is not None:
            inference["temperature"] = temperature
        response = self.client.converse(
            modelId=model_id,
            system=[{"text": system}],
            messages=[{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages],
            inferenceConfig=inference,
        )
        try:
            blocks = response["output"]["message"]["content"]
            usage = response["usage"]
        except (KeyError, TypeError) as e:
            raise LLMError(f"unexpected Converse response shape: missing {e}") from e
        return Reply(
            text="".join(b.get("text", "") for b in blocks),
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
            stop_reason=response.get("stopReason"),
        )

    def is_retryable(self, exc: Exception) -> bool:
        from botocore.exceptions import ClientError, ConnectionError, ReadTimeoutError

        if isinstance(exc, (ReadTimeoutError, ConnectionError)):
            return True
        return isinstance(exc, ClientError) and _bedrock_code(exc) in BEDROCK_RETRY_CODES

    def rejects_temperature(self, exc: Exception) -> bool:
        from botocore.exceptions import ClientError

        return (
            isinstance(exc, ClientError)
            and _bedrock_code(exc) == "ValidationException"
            and "temperature" in str(exc).lower()
        )


def _bedrock_code(exc: Any) -> str:
    return exc.response.get("Error", {}).get("Code", "")


BACKENDS = {"anthropic": AnthropicBackend, "bedrock": BedrockBackend}


# --- Shared helpers ------------------------------------------------------------


def parse_json(text: str) -> Any:
    """The reply as a dict or list if it is JSON (a ```json fence is allowed), else None."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    try:
        value = json.loads(s)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, (dict, list)) else None


def cost_usd(
    provider: str, model_id: str, input_tokens: int, output_tokens: int, prices: dict[str, Any]
) -> float | None:
    """Cost from configs/default.yaml prices, or None when no price applies."""
    p = prices.get(provider) or {}
    price_in, price_out = p.get("input_per_mtok_usd"), p.get("output_per_mtok_usd")
    if price_in is None or price_out is None:
        return None
    if p.get("model") and p["model"] not in model_id:
        return None  # prices are for a different model; never report a guessed cost
    if p.get("max_input_tokens") and input_tokens > p["max_input_tokens"]:
        return None  # long prompts are billed at a different tier
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def check_messages(messages: list[dict[str, str]]) -> None:
    if not messages or messages[-1]["role"] != "user":
        raise ValueError("messages must end with a user turn")
    for m in messages:
        if m["role"] not in ROLES:
            raise ValueError(f"unknown role {m['role']!r}; expected one of {ROLES}")


def cache_key(request: dict[str, Any]) -> str:
    """SHA-256 of the request; any change to provider, model, prompt or settings is a new key."""
    blob = json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# --- The client ----------------------------------------------------------------


class LLMClient:
    def __init__(
        self,
        cfg: dict[str, Any],
        backend: Backend | None = None,
        cache_dir: Path = paths.LLM_CACHE,
        use_cache: bool = True,
        sleep=time.sleep,
    ):
        provider = backend.name if backend else (cfg.get("llm_provider") or env("LLM_PROVIDER"))
        if provider not in BACKENDS:
            raise RuntimeError(
                f"LLM_PROVIDER must be one of {sorted(BACKENDS)}; set it in .env (got {provider!r})"
            )
        self.provider = provider
        self.model_id = cfg.get("generator_model") or env(MODEL_ID_ENV[provider], required=True)
        self.generation = cfg["generation"]
        validate_generation(self.generation)
        self.backend = backend or BACKENDS[provider](timeout_s=self.generation.get("timeout_s", 60))
        sends = self.backend.sends_thinking
        self.thinking = self.generation["thinking"] if sends else None
        self.effort = self.generation.get("effort") if sends else None
        self.cache_dir = Path(cache_dir)
        self.use_cache = use_cache
        self._sleep = sleep
        self._temperature = self.generation.get("temperature")

    def complete(
        self, system: str, messages: list[dict[str, str]], max_tokens: int | None = None
    ) -> LLMResult:
        check_messages(messages)
        max_tokens = max_tokens or self.generation["max_tokens"]
        key = cache_key(
            {
                "provider": self.provider,
                "model": self.model_id,
                "system": system,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": self.generation.get("temperature"),
                "thinking": self.thinking,
                "effort": self.effort,
            }
        )

        if self.use_cache:
            hit = self._read_cache(key)
            if hit is not None:
                return self._result(hit, cached=True)

        start = time.perf_counter()
        reply = self._send_with_retries(system, messages, max_tokens)
        record = {
            **asdict(reply),
            "latency_ms": round((time.perf_counter() - start) * 1000),
            "temperature": self._temperature,
        }
        if self.use_cache:
            self._write_cache(key, record)
        return self._result(record, cached=False)

    def _send_with_retries(self, system, messages, max_tokens) -> Reply:
        max_retries = self.generation.get("max_retries", 5)
        base = self.generation.get("retry_base_delay_s", 1.0)
        cap = self.generation.get("retry_max_delay_s", 30.0)
        retries = 0
        while True:
            try:
                return self.backend.send(
                    self.model_id,
                    system,
                    messages,
                    max_tokens,
                    self._temperature,
                    self.thinking,
                    self.effort,
                )
            except LLMError:
                raise
            except Exception as e:
                if self._temperature is not None and self.backend.rejects_temperature(e):
                    log.warning(
                        "%s rejected temperature=%s; sending without it from now on",
                        self.model_id,
                        self._temperature,
                    )
                    self._temperature = None
                    continue
                if self.backend.is_retryable(e) and retries < max_retries:
                    delay = min(base * 2**retries, cap) + random.uniform(0, base)
                    retries += 1
                    log.warning(
                        "%s (%s); retry %d/%d in %.1fs",
                        self.provider,
                        type(e).__name__,
                        retries,
                        max_retries,
                        delay,
                    )
                    self._sleep(delay)
                    continue
                raise LLMError(f"{self.provider} call failed: {type(e).__name__}: {e}") from e

    def _result(self, record: dict[str, Any], cached: bool) -> LLMResult:
        # Cost is recomputed from tokens so a price fixed in config applies to cached runs too.
        truncated = record["stop_reason"] == "max_tokens"
        return LLMResult(
            text=record["text"],
            parsed_json=None if truncated else parse_json(record["text"]),
            input_tokens=record["input_tokens"],
            output_tokens=record["output_tokens"],
            latency_ms=record["latency_ms"],
            cost_usd=cost_usd(
                self.provider,
                self.model_id,
                record["input_tokens"],
                record["output_tokens"],
                self.generation.get("prices", {}),
            ),
            provider=self.provider,
            model_id=self.model_id,
            stop_reason=record["stop_reason"],
            truncated=truncated,
            temperature=record["temperature"],
            thinking=self.thinking,
            effort=self.effort,
            cached=cached,
        )

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, key: str) -> dict[str, Any] | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None  # a corrupt entry is treated as a miss and overwritten

    def _write_cache(self, key: str, record: dict[str, Any]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path(key).with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False))
        tmp.replace(self._cache_path(key))


def make_judge(client: LLMClient) -> Callable[[str, str], str]:
    """A `judge(system, user) -> str` for src/generate/claim_check.py, on the same client.

    It shares the client's settings and cache. A truncated or refused reply raises
    LLMError, which claim_check treats as a failed check (fail closed).
    """

    def judge(system: str, user: str) -> str:
        result = client.complete(system, [{"role": "user", "content": user}])
        if result.truncated:
            raise LLMError("judge reply was cut off at max_tokens")
        if result.stop_reason == "refusal":
            raise LLMError("judge request was refused by the model")
        return result.text

    return judge


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM client utilities.")
    parser.add_argument("--smoke", action="store_true", help='send "Say hi" once, live, no cache')
    parser.add_argument("--config", default="b2")
    args = parser.parse_args()
    if not args.smoke:
        parser.error("nothing to do; use --smoke")

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    llm = LLMClient(load_config(args.config), use_cache=False)
    # Default max_tokens: models that think by default spend output tokens before the text.
    r = llm.complete("Reply briefly.", [{"role": "user", "content": "Say hi"}])
    cost = "unknown (no price set)" if r.cost_usd is None else f"${r.cost_usd:.8f}"
    print(f"reply:   {r.text}")
    print(f"tokens:  {r.input_tokens} in, {r.output_tokens} out")
    print(f"latency: {r.latency_ms} ms")
    print(f"cost:    {cost}")


if __name__ == "__main__":
    main()
