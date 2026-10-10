"""The generation step: follow-up rewrite and grounded answer generation.

    rewrite_followup(question, history, client) -> str
    generate(question, passages, cfg, client, closed_book=False, instructions=None) -> Generation

generate() numbers the reranked passages, calls the LLM with the prompt from
prompt.py, parses the JSON reply, repairs it once with one more call if needed,
and maps passage numbers to chunk IDs. It never raises on a model problem: a
failed call, a truncated or refused reply, or JSON that is still invalid after
the repair all return status "error", and answer() refuses (fail closed).

A sentence that cites a passage number outside the list keeps a visible
"invalid-passage-<n>" marker instead of a chunk ID, so citation_validity fails
it rather than the citation silently disappearing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, TypedDict

from src.common.contracts import Sentence
from src.generate import prompt
from src.generate.llm import LLMError, LLMResult

GenerationStatus = Literal["answered", "insufficient", "error"]


class GenerationUsage(TypedDict):
    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # None when any call had no price (see configs/default.yaml)
    latency_ms: int


class Generation(TypedDict):
    status: GenerationStatus
    sentences: list[Sentence]
    raw_text: str
    error: str | None
    usage: GenerationUsage


class LLM(Protocol):
    def complete(
        self, system: str, messages: list[dict[str, str]], max_tokens: int | None = None
    ) -> LLMResult: ...


class _Usage:
    """Adds up tokens, cost and latency over the calls of one generation."""

    def __init__(self) -> None:
        self.input_tokens = self.output_tokens = self.latency_ms = 0
        self.cost_usd: float | None = 0.0

    def add(self, r: LLMResult) -> None:
        self.input_tokens += r.input_tokens
        self.output_tokens += r.output_tokens
        self.latency_ms += r.latency_ms
        self.cost_usd = (
            None if self.cost_usd is None or r.cost_usd is None else (self.cost_usd + r.cost_usd)
        )

    def as_dict(self) -> GenerationUsage:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
        }


def invalid_passage_marker(n: Any) -> str:
    return f"invalid-passage-{n}"


def rewrite_followup(question: str, history: Sequence[Mapping[str, str]], client: LLM) -> str:
    """A standalone version of a follow-up question; one LLM call, only with history.

    history is a list of {"role": "user" | "assistant", "content": str} turns. If the
    call fails or returns nothing usable, the original question is returned: the
    rewrite only helps retrieval, so it must never block an answer.
    """
    if not history:
        return question
    try:
        r = client.complete(
            prompt.REWRITE_SYSTEM_PROMPT,
            [{"role": "user", "content": prompt.build_rewrite_message(question, history)}],
            max_tokens=256,
        )
    except LLMError:
        return question
    rewritten = r.text.strip().strip('"').strip()
    if r.truncated or r.stop_reason == "refusal" or not rewritten or "\n" in rewritten:
        return question
    return rewritten


Parsed = tuple[GenerationStatus, list[tuple[str, list[Any]]]]


def _parse(reply: Any, closed_book: bool) -> Parsed | None:
    """(status, [(sentence text, passage numbers)]) from a reply, or None if the shape is wrong."""
    if not isinstance(reply, dict):
        return None
    if reply.get("insufficient") is True:
        return "insufficient", []
    items = reply.get("sentences")
    if not isinstance(items, list):
        return None
    sentences: list[tuple[str, list[Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        numbers = [] if closed_book else item.get("passages")
        if not isinstance(numbers, list):
            return None
        sentences.append((text.strip(), numbers))
    if not sentences:
        return "insufficient", []
    return "answered", sentences


def _passage_number(n: Any) -> int | None:
    """An int passage number; digit strings such as "2" are accepted."""
    if isinstance(n, bool):
        return None
    if isinstance(n, int):
        return n
    if isinstance(n, str) and n.strip().isdigit():
        return int(n.strip())
    return None


def _to_sentences(parsed: list[tuple[str, list[Any]]], chunk_ids: list[str]) -> list[Sentence]:
    """Replace passage numbers with chunk IDs (1-based), keeping order, without repeats."""
    sentences: list[Sentence] = []
    for text, numbers in parsed:
        ids: list[str] = []
        for raw in numbers:
            n = _passage_number(raw)
            cid = (
                chunk_ids[n - 1] if n and 1 <= n <= len(chunk_ids) else invalid_passage_marker(raw)
            )
            if cid not in ids:
                ids.append(cid)
        sentences.append({"text": text, "chunk_ids": ids})
    return sentences


def _result(
    status: GenerationStatus,
    usage: _Usage,
    raw_text: str = "",
    sentences: list[Sentence] | None = None,
    error: str | None = None,
) -> Generation:
    return {
        "status": status,
        "sentences": sentences or [],
        "raw_text": raw_text,
        "error": error,
        "usage": usage.as_dict(),
    }


def _bad_reply(r: LLMResult) -> str | None:
    """Why a reply cannot be used at all, or None."""
    if r.truncated:
        return "reply was cut off at max_tokens"
    if r.stop_reason == "refusal":
        return "the model declined the request"
    return None


def generate(
    question: str,
    passages: Sequence[Mapping[str, Any]],
    cfg: Mapping[str, Any],
    client: LLM,
    closed_book: bool = False,
    instructions: str | None = None,
) -> Generation:
    """Answer from the numbered passages (or closed-book for B0) as cited sentences.

    cfg is the run config; generation.max_tokens caps each call. instructions is
    optional extra prompt text from the router (pilot_unclear only).
    """
    usage = _Usage()
    if not closed_book and not passages:
        return _result("insufficient", usage)

    if closed_book:
        system = prompt.CLOSED_BOOK_SYSTEM_PROMPT
        user = prompt.build_closed_book_message(question, instructions)
        chunk_ids: list[str] = []
    else:
        system = prompt.SYSTEM_PROMPT
        user = prompt.build_user_message(question, passages, instructions)
        chunk_ids = [p["chunk_id"] for p in passages]
    max_tokens = cfg.get("generation", {}).get("max_tokens")
    messages = [{"role": "user", "content": user}]

    try:
        first = client.complete(system, messages, max_tokens=max_tokens)
    except LLMError as e:
        return _result("error", usage, error=str(e))
    usage.add(first)
    if problem := _bad_reply(first):
        return _result("error", usage, first.text, error=problem)

    parsed = _parse(first.parsed_json, closed_book)
    raw_text = first.text
    if parsed is None:
        # One repair attempt: show the model its reply and ask for the required shape.
        repair_messages = messages + [
            {"role": "assistant", "content": first.text or "(empty reply)"},
            {"role": "user", "content": prompt.REPAIR_INSTRUCTION},
        ]
        try:
            second = client.complete(system, repair_messages, max_tokens=max_tokens)
        except LLMError as e:
            return _result("error", usage, raw_text, error=f"repair call failed: {e}")
        usage.add(second)
        raw_text = second.text
        if problem := _bad_reply(second):
            return _result("error", usage, raw_text, error=f"repair {problem}")
        parsed = _parse(second.parsed_json, closed_book)
        if parsed is None:
            return _result(
                "error", usage, raw_text, error="reply is not valid JSON in the required shape"
            )

    status, items = parsed
    return _result(status, usage, raw_text, _to_sentences(items, chunk_ids))
