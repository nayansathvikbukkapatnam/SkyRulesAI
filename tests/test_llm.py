"""LLM client tests. Fake clients stand in for the SDKs: no network, no keys."""

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from src.generate.llm import (
    AnthropicBackend,
    BedrockBackend,
    LLMClient,
    LLMConfigError,
    LLMError,
    cache_key,
    cost_usd,
    make_judge,
    parse_json,
)

PRICES = {
    "anthropic": {
        "model": "claude-haiku-5-5",
        "input_per_mtok_usd": 0.10,
        "output_per_mtok_usd": 0.50,
        "max_input_tokens": 100000,
    },
    "bedrock": {
        "model": "claude-haiku-5-5",
        "input_per_mtok_usd": None,
        "output_per_mtok_usd": None,
        "max_input_tokens": 100000,
    },
}
GENERATION = {
    "temperature": None,
    "thinking": "disabled",
    "effort": "medium",
    "max_tokens": 1024,
    "timeout_s": 60,
    "max_retries": 3,
    "retry_base_delay_s": 1.0,
    "retry_max_delay_s": 30.0,
    "prices": PRICES,
}
MESSAGES = [{"role": "user", "content": "How high can I fly?"}]
URL = "https://api.anthropic.com/v1/messages"


def cfg(provider, model="claude-haiku-5-5", **generation):
    return {
        "llm_provider": provider,
        "generator_model": model,
        "generation": {**GENERATION, **generation},
    }


def api_status_error(cls, status, message="error"):
    response = httpx2.Response(status, request=httpx2.Request("POST", URL))
    return cls(message, response=response, body=None)


def bedrock_error(code, message="error"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


class FakeMessages:
    """Stands in for anthropic.Anthropic().messages; raises queued errors first."""

    def __init__(self, text="400 feet.", errors=(), thinking=False, stop_reason="end_turn"):
        self.calls, self.errors, self.text, self.thinking = [], list(errors), text, thinking
        self.stop_reason = stop_reason

    def create(self, **params):
        self.calls.append(params)
        if self.errors:
            raise self.errors.pop(0)
        content = [SimpleNamespace(type="text", text=self.text)]
        if self.thinking:
            content.insert(0, SimpleNamespace(type="thinking", thinking=""))
        return SimpleNamespace(
            content=content,
            usage=SimpleNamespace(input_tokens=1200, output_tokens=40),
            stop_reason=self.stop_reason,
        )


class FakeBedrock:
    def __init__(self, text="400 feet.", errors=(), stop_reason="end_turn"):
        self.calls, self.errors, self.text, self.stop_reason = [], list(errors), text, stop_reason

    def converse(self, **request):
        self.calls.append(request)
        if self.errors:
            raise self.errors.pop(0)
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": self.text}]}},
            "usage": {"inputTokens": 1200, "outputTokens": 40},
            "stopReason": self.stop_reason,
        }


@pytest.fixture
def sleeps():
    return []


def anthropic_llm(tmp_path, sleeps, gen=None, **fake_kwargs):
    fake = FakeMessages(**fake_kwargs)
    backend = AnthropicBackend(client=SimpleNamespace(messages=fake))
    llm = LLMClient(
        cfg("anthropic", **(gen or {})), backend=backend, cache_dir=tmp_path, sleep=sleeps.append
    )
    return llm, fake


def bedrock_llm(tmp_path, sleeps, gen=None, **fake_kwargs):
    fake = FakeBedrock(**fake_kwargs)
    llm = LLMClient(
        cfg("bedrock", "anthropic.claude-haiku-5-5", **(gen or {})),
        backend=BedrockBackend(client=fake),
        cache_dir=tmp_path,
        sleep=sleeps.append,
    )
    return llm, fake


# --- Both backends -------------------------------------------------------------


def test_anthropic_backend_sends_messages_api_request(tmp_path, sleeps):
    llm, fake = anthropic_llm(tmp_path, sleeps)
    result = llm.complete("system prompt", MESSAGES)
    assert fake.calls == [
        {
            "model": "claude-haiku-5-5",
            "max_tokens": 1024,
            "system": "system prompt",
            "messages": MESSAGES,
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "medium"},
        }
    ]
    assert (result.thinking, result.effort, result.temperature) == ("disabled", "medium", None)
    assert (result.provider, result.model_id, result.text) == (
        "anthropic",
        "claude-haiku-5-5",
        "400 feet.",
    )
    assert (result.input_tokens, result.output_tokens, result.stop_reason) == (1200, 40, "end_turn")
    assert result.latency_ms >= 0 and result.cached is False


def test_anthropic_backend_skips_thinking_blocks(tmp_path, sleeps):
    llm, _ = anthropic_llm(tmp_path, sleeps, thinking=True)
    assert llm.complete("s", MESSAGES).text == "400 feet."


def test_bedrock_backend_sends_converse_request(tmp_path, sleeps):
    llm, fake = bedrock_llm(tmp_path, sleeps)
    result = llm.complete("system prompt", MESSAGES)
    assert fake.calls == [
        {
            "modelId": "anthropic.claude-haiku-5-5",
            "system": [{"text": "system prompt"}],
            "messages": [{"role": "user", "content": [{"text": "How high can I fly?"}]}],
            "inferenceConfig": {"maxTokens": 1024},  # temperature null: never sent
        }
    ]
    assert (result.provider, result.text, result.input_tokens) == ("bedrock", "400 feet.", 1200)


def test_history_roles_pass_through_in_order(tmp_path, sleeps):
    llm, fake = anthropic_llm(tmp_path, sleeps)
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    llm.complete("s", history)
    assert [m["role"] for m in fake.calls[0]["messages"]] == ["user", "assistant", "user"]


@pytest.mark.parametrize(
    "messages",
    [[], [{"role": "assistant", "content": "a"}], [{"role": "system", "content": "x"}]],
)
def test_bad_messages_are_rejected(tmp_path, sleeps, messages):
    llm, _ = anthropic_llm(tmp_path, sleeps)
    with pytest.raises(ValueError):
        llm.complete("s", messages)


# --- Provider selection ------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "backend_cls", "model_key"),
    [
        ("anthropic", AnthropicBackend, "ANTHROPIC_MODEL_ID"),
        ("bedrock", BedrockBackend, "BEDROCK_MODEL_ID"),
    ],
)
def test_provider_selects_backend_and_model_key(provider, backend_cls, model_key, monkeypatch):
    built = []
    monkeypatch.setattr(backend_cls, "__init__", lambda self, **kw: built.append(kw))
    monkeypatch.setenv(model_key, f"{provider}-model")
    llm = LLMClient({"llm_provider": provider, "generator_model": None, "generation": GENERATION})
    assert isinstance(llm.backend, backend_cls)
    assert (llm.provider, llm.model_id) == (provider, f"{provider}-model")
    assert built == [{"timeout_s": 60}]


@pytest.mark.parametrize("provider", [None, "openai"])
def test_missing_or_unknown_provider_gives_a_clear_error(provider, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setattr("src.common.config._dotenv_loaded", True)  # ignore a local .env
    with pytest.raises(RuntimeError, match="LLM_PROVIDER must be one of"):
        LLMClient({"llm_provider": provider, "generator_model": None, "generation": GENERATION})


def test_missing_model_id_gives_a_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL_ID", raising=False)
    monkeypatch.setattr("src.common.config._dotenv_loaded", True)
    with pytest.raises(RuntimeError, match="ANTHROPIC_MODEL_ID is not set"):
        LLMClient({"llm_provider": "anthropic", "generator_model": None, "generation": GENERATION})


# --- JSON parsing --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"insufficient": true}', {"insufficient": True}),
        ('  [{"text": "a", "chunk_ids": ["x"]}]\n', [{"text": "a", "chunk_ids": ["x"]}]),
        ('```json\n{"insufficient": true}\n```', {"insufficient": True}),
        ("Hi there!", None),
        ("42", None),
        ('{"unclosed": ', None),
    ],
)
def test_parse_json(text, expected):
    assert parse_json(text) == expected


def test_result_carries_parsed_json(tmp_path, sleeps):
    llm, _ = anthropic_llm(tmp_path, sleeps, text='{"insufficient": true}')
    assert llm.complete("s", MESSAGES).parsed_json == {"insufficient": True}


# --- Retries ---------------------------------------------------------------------------


def test_anthropic_429_is_retried_with_backoff(tmp_path, sleeps):
    llm, fake = anthropic_llm(
        tmp_path, sleeps, errors=[api_status_error(anthropic.RateLimitError, 429)] * 2
    )
    result = llm.complete("s", MESSAGES)
    assert result.text == "400 feet." and len(fake.calls) == 3
    assert len(sleeps) == 2
    assert 1.0 <= sleeps[0] < 2.0 and 2.0 <= sleeps[1] < 3.0  # 1 s, then 2 s, plus jitter


@pytest.mark.parametrize(
    "error",
    [
        api_status_error(anthropic.APIStatusError, 529, "overloaded"),
        api_status_error(anthropic.InternalServerError, 500),
        anthropic.APITimeoutError(request=httpx2.Request("POST", URL)),
    ],
    ids=["overloaded-529", "server-500", "timeout"],
)
def test_anthropic_overloaded_server_and_timeout_errors_are_retried(tmp_path, sleeps, error):
    llm, fake = anthropic_llm(tmp_path, sleeps, errors=[error])
    assert llm.complete("s", MESSAGES).text == "400 feet."
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    "error",
    [
        bedrock_error("ThrottlingException"),
        bedrock_error("ServiceUnavailableException"),
        ReadTimeoutError(endpoint_url="https://bedrock-runtime"),
    ],
    ids=["throttling", "unavailable", "timeout"],
)
def test_bedrock_throttling_and_timeouts_are_retried(tmp_path, sleeps, error):
    llm, fake = bedrock_llm(tmp_path, sleeps, errors=[error])
    assert llm.complete("s", MESSAGES).text == "400 feet."
    assert len(fake.calls) == 2


def test_retries_stop_after_max_retries(tmp_path, sleeps):
    errors = [api_status_error(anthropic.RateLimitError, 429)] * 10
    llm, fake = anthropic_llm(tmp_path, sleeps, errors=errors)
    with pytest.raises(LLMError, match="RateLimitError"):
        llm.complete("s", MESSAGES)
    assert len(fake.calls) == 1 + GENERATION["max_retries"]
    assert list(tmp_path.iterdir()) == []  # failures are never cached


@pytest.mark.parametrize(
    "error",
    [
        api_status_error(anthropic.AuthenticationError, 401),
        api_status_error(anthropic.BadRequestError, 400, "max_tokens too large"),
    ],
    ids=["auth-401", "bad-request-400"],
)
def test_client_errors_are_not_retried(tmp_path, sleeps, error):
    llm, fake = anthropic_llm(tmp_path, sleeps, errors=[error])
    with pytest.raises(LLMError):
        llm.complete("s", MESSAGES)
    assert len(fake.calls) == 1 and sleeps == []


# --- Temperature -----------------------------------------------------------------------


def test_null_temperature_is_never_sent(tmp_path, sleeps):
    llm, fake = anthropic_llm(tmp_path, sleeps)
    bedrock, bfake = bedrock_llm(tmp_path / "b", sleeps)
    assert llm.complete("s", MESSAGES).temperature is None
    assert bedrock.complete("s", MESSAGES).temperature is None
    assert "extra_body" not in fake.calls[0]
    assert "temperature" not in bfake.calls[0]["inferenceConfig"]


def test_null_temperature_has_no_drop_fallback(tmp_path, sleeps):
    # With nothing sent, a 400 that mentions temperature is a real error, not a retry.
    rejected = api_status_error(anthropic.BadRequestError, 400, "temperature: bad value")
    llm, fake = anthropic_llm(tmp_path, sleeps, errors=[rejected])
    with pytest.raises(LLMError):
        llm.complete("s", MESSAGES)
    assert len(fake.calls) == 1


def test_explicit_temperature_is_sent_in_the_request_body(tmp_path, sleeps):
    llm, fake = anthropic_llm(tmp_path, sleeps, gen={"temperature": 0})
    result = llm.complete("s", MESSAGES)
    # anthropic 1.x removed temperature from create(), so it goes in the body
    assert fake.calls[0]["extra_body"] == {"temperature": 0}
    assert result.temperature == 0


def test_explicit_temperature_rejected_by_anthropic_is_dropped_once(tmp_path, sleeps):
    rejected = api_status_error(
        anthropic.BadRequestError, 400, "temperature: non-default values are not supported"
    )
    llm, fake = anthropic_llm(tmp_path, sleeps, gen={"temperature": 0}, errors=[rejected])
    first = llm.complete("s", MESSAGES)
    llm.complete("s", [{"role": "user", "content": "another question"}])
    assert fake.calls[0]["extra_body"] == {"temperature": 0}
    assert all("extra_body" not in call for call in fake.calls[1:])
    assert first.temperature is None and sleeps == []


def test_explicit_temperature_rejected_by_bedrock_is_dropped(tmp_path, sleeps):
    rejected = bedrock_error("ValidationException", "temperature is not supported for this model")
    llm, fake = bedrock_llm(tmp_path, sleeps, gen={"temperature": 0}, errors=[rejected])
    assert llm.complete("s", MESSAGES).temperature is None
    assert fake.calls[0]["inferenceConfig"]["temperature"] == 0
    assert "temperature" not in fake.calls[1]["inferenceConfig"]


# --- Thinking and effort ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("gen", "expected"),
    [
        (
            {"thinking": "disabled", "effort": "low"},
            {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}},
        ),
        (
            {"thinking": "adaptive", "effort": "xhigh"},
            {"thinking": {"type": "adaptive"}, "output_config": {"effort": "xhigh"}},
        ),
        ({"thinking": "adaptive", "effort": None}, {"thinking": {"type": "adaptive"}}),
    ],
    ids=["disabled-low", "adaptive-xhigh", "adaptive-no-effort"],
)
def test_anthropic_thinking_and_effort_request_shape(tmp_path, sleeps, gen, expected):
    llm, fake = anthropic_llm(tmp_path, sleeps, gen=gen)
    result = llm.complete("s", MESSAGES)
    sent = {k: fake.calls[0][k] for k in ("thinking", "output_config") if k in fake.calls[0]}
    assert sent == expected
    assert (result.thinking, result.effort) == (gen["thinking"], gen["effort"])


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_disabled_thinking_with_xhigh_or_max_effort_fails_before_any_call(tmp_path, effort):
    fake = FakeMessages()
    with pytest.raises(LLMConfigError, match="cannot be combined with effort"):
        LLMClient(
            cfg("anthropic", thinking="disabled", effort=effort),
            backend=AnthropicBackend(client=SimpleNamespace(messages=fake)),
            cache_dir=tmp_path,
        )
    assert fake.calls == []


@pytest.mark.parametrize(
    "gen",
    [{"thinking": "enabled"}, {"thinking": None}, {"effort": "extreme"}],
    ids=["thinking-enabled", "thinking-null", "effort-unknown"],
)
def test_invalid_thinking_or_effort_is_a_config_error(tmp_path, gen):
    with pytest.raises(LLMConfigError):
        LLMClient(
            cfg("anthropic", **gen),
            backend=AnthropicBackend(client=SimpleNamespace(messages=FakeMessages())),
            cache_dir=tmp_path,
        )


def test_bedrock_does_not_send_or_record_thinking_or_effort(tmp_path, sleeps):
    llm, fake = bedrock_llm(tmp_path, sleeps)
    result = llm.complete("s", MESSAGES)
    assert set(fake.calls[0]) == {"modelId", "system", "messages", "inferenceConfig"}
    assert (result.thinking, result.effort) == (None, None)


def test_thinking_and_effort_change_the_cache_key(tmp_path, sleeps):
    llm, fake = anthropic_llm(tmp_path, sleeps)
    llm.complete("s", MESSAGES)
    other, other_fake = anthropic_llm(tmp_path, sleeps, gen={"effort": "low"})
    assert other.complete("s", MESSAGES).cached is False
    assert len(other_fake.calls) == 1


# --- Truncation --------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["anthropic", "bedrock"])
def test_max_tokens_stop_marks_truncated_and_drops_parsed_json(tmp_path, sleeps, backend):
    make = anthropic_llm if backend == "anthropic" else bedrock_llm
    llm, _ = make(tmp_path, sleeps, text='{"insufficient": true}', stop_reason="max_tokens")
    result = llm.complete("s", MESSAGES)
    assert result.truncated is True
    assert result.parsed_json is None
    assert result.text == '{"insufficient": true}'  # the raw text is kept for the log


def test_truncated_stays_truncated_on_a_cache_hit(tmp_path, sleeps):
    llm, _ = anthropic_llm(tmp_path, sleeps, text='{"a": 1}', stop_reason="max_tokens")
    llm.complete("s", MESSAGES)
    hit = llm.complete("s", MESSAGES)
    assert hit.cached and hit.truncated and hit.parsed_json is None


def test_normal_stop_is_not_truncated(tmp_path, sleeps):
    llm, _ = anthropic_llm(tmp_path, sleeps, text='{"a": 1}')
    result = llm.complete("s", MESSAGES)
    assert result.truncated is False and result.parsed_json == {"a": 1}


# --- Judge for claim_check ---------------------------------------------------------------


def test_judge_sends_system_and_user_through_the_same_client(tmp_path, sleeps):
    llm, fake = anthropic_llm(tmp_path, sleeps, text='{"supported": true, "reason": "ok"}')
    judge = make_judge(llm)
    reply = judge("judge system", "sentence and passages")
    assert reply == '{"supported": true, "reason": "ok"}'
    assert fake.calls[0]["system"] == "judge system"
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "sentence and passages"}]
    assert fake.calls[0]["thinking"] == {"type": "disabled"}  # same settings as the client
    judge("judge system", "sentence and passages")
    assert len(fake.calls) == 1  # same client, same cache


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_judge_raises_on_truncated_or_refused_reply(tmp_path, sleeps, stop_reason):
    llm, _ = anthropic_llm(tmp_path, sleeps, text="yes", stop_reason=stop_reason)
    with pytest.raises(LLMError):
        make_judge(llm)("s", "u")


def test_judge_raises_llm_error_when_the_call_fails(tmp_path, sleeps):
    llm, _ = anthropic_llm(
        tmp_path, sleeps, errors=[api_status_error(anthropic.AuthenticationError, 401)]
    )
    with pytest.raises(LLMError):
        make_judge(llm)("s", "u")


# --- Cost --------------------------------------------------------------------------------


def test_cost_math_for_haiku_on_the_claude_api():
    # 1,200 input tokens at $0.10/M plus 40 output tokens at $0.50/M
    assert cost_usd("anthropic", "claude-haiku-5-5", 1200, 40, PRICES) == pytest.approx(0.00014)
    # At the 100k-token tier limit: 100,000 x $0.10/M + 1,000,000 x $0.50/M = $0.51
    assert cost_usd("anthropic", "claude-haiku-5-5", 100_000, 1_000_000, PRICES) == pytest.approx(
        0.51
    )


@pytest.mark.parametrize(
    ("provider", "model", "input_tokens"),
    [
        ("bedrock", "anthropic.claude-haiku-5-5", 1200),  # Bedrock price not filled in
        ("anthropic", "claude-sonnet-5-5", 1200),  # prices are for a different model
        ("anthropic", "claude-haiku-5-5", 100_001),  # long-prompt tier not configured
        ("other", "claude-haiku-5-5", 1200),  # no prices for this provider
    ],
)
def test_cost_is_none_when_no_price_applies(provider, model, input_tokens):
    assert cost_usd(provider, model, input_tokens, 40, PRICES) is None


def test_result_cost_uses_provider_prices(tmp_path, sleeps):
    llm, _ = anthropic_llm(tmp_path, sleeps)
    assert llm.complete("s", MESSAGES).cost_usd == pytest.approx(0.00014)
    bedrock, _ = bedrock_llm(tmp_path / "b", sleeps)
    assert bedrock.complete("s", MESSAGES).cost_usd is None


# --- Cache -------------------------------------------------------------------------------


def test_cache_hit_skips_the_api_and_keeps_original_numbers(tmp_path, sleeps):
    llm, fake = anthropic_llm(tmp_path, sleeps)
    first = llm.complete("s", MESSAGES)
    second = llm.complete("s", MESSAGES)
    assert len(fake.calls) == 1 and second.cached is True
    assert (second.text, second.input_tokens, second.latency_ms, second.cost_usd) == (
        first.text,
        first.input_tokens,
        first.latency_ms,
        first.cost_usd,
    )


def test_cache_can_be_turned_off(tmp_path, sleeps):
    fake = FakeMessages()
    llm = LLMClient(
        cfg("anthropic"),
        backend=AnthropicBackend(client=SimpleNamespace(messages=fake)),
        cache_dir=tmp_path,
        use_cache=False,
    )
    llm.complete("s", MESSAGES)
    llm.complete("s", MESSAGES)
    assert len(fake.calls) == 2 and list(tmp_path.iterdir()) == []


def test_any_request_change_gives_a_new_cache_key():
    base = {"provider": "anthropic", "model": "m", "system": "s", "messages": MESSAGES}
    changes = [
        {"provider": "bedrock"},
        {"model": "other"},
        {"system": "other"},
        {"messages": [{"role": "user", "content": "x"}]},
        {"max_tokens": 5},
    ]
    keys = {cache_key(base)} | {cache_key({**base, **c}) for c in changes}
    assert len(keys) == 1 + len(changes)


def test_corrupt_cache_entry_is_a_miss(tmp_path, sleeps):
    llm, fake = anthropic_llm(tmp_path, sleeps)
    llm.complete("s", MESSAGES)
    (entry,) = tmp_path.iterdir()
    entry.write_text("{not json")
    assert llm.complete("s", MESSAGES).cached is False and len(fake.calls) == 2


def test_cache_file_holds_only_the_response(tmp_path, sleeps, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("AWS_PROFILE", "secret-profile-name")
    llm, _ = anthropic_llm(tmp_path, sleeps)
    llm.complete("s", MESSAGES)
    (entry,) = tmp_path.iterdir()
    raw = entry.read_text()
    assert set(json.loads(raw)) == {
        "text",
        "input_tokens",
        "output_tokens",
        "stop_reason",
        "latency_ms",
        "temperature",
    }
    assert "sk-test" not in raw and "secret-profile-name" not in raw


def test_default_config_generation_settings(monkeypatch):
    from src.common.config import load_config

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    for name in ["b0", "b2", "b3"]:
        gen = load_config(name)["generation"]
        assert (gen["temperature"], gen["thinking"], gen["effort"]) == (None, "disabled", "medium")
