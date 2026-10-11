"""The answer() pipeline (CLAUDE.md section 5): question in, checked and cited Answer out.

    answer(question, history, config, pilot_type="not_sure", on_stage=None, client=None) -> Answer

Steps, in order:
 1. Follow-up rewrite (only with history).
 2. Route: tag, referral, injection text stripped, extra instructions.
 3. Retrieve and rerank (skipped in closed-book mode, B0).
 4. Evidence gate on the top rerank score (only when gate is on and a threshold is set).
 5. Generate from the passages.
 6. Checks named in the config's `checks` (checks.py and claim_check.py).
 7. Drop each sentence that fails a check (proposal sections 7.4 and 8). Fail closed:
    refuse on insufficient, a generation error, a check or judge error, no sentence left,
    or more than MAX_DROPPED_SENTENCES dropped.
 8. Referral: attached for location and high-stakes tags, also on refusals.
 9. Citations from the cited chunks, per-stage latency and total cost.
10. Log the query.

Model and check problems end in a refusal (status "refused", reason in
trace["refusal_reason"]). Infrastructure problems (a missing index, a crash in
retrieval) raise, so an eval run counts them as errors instead of refusals.

on_stage(name, status, ms) is called for the stages route, retrieve, rerank,
generate and check, with status "started", then "done", "stopped" (the answer
stops there) or "skipped" (not used by this config).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from src.common.contracts import Answer, Citation, Sentence
from src.generate import checks as answer_checks
from src.generate import claim_check
from src.generate.generate import generate, rewrite_followup
from src.generate.llm import LLMClient, LLMResult, make_judge
from src.generate.log import log_query
from src.generate.router import route

STAGES = ("route", "retrieve", "rerank", "generate", "check")
OnStage = Callable[[str, str, float], None]

# Proposal 7.4: a sentence that fails a check is dropped, and the answer is refused when
# more than one is dropped (or none is left).
MAX_DROPPED_SENTENCES = 1

# A judge that errored or replied with something unparseable is a check error, not a
# verdict: the answer is refused (fail closed), never answered without that sentence.
JUDGE_ERROR_PREFIXES = ("judge error", "unparseable judge reply")

# Names shown on the checks row of the answer card (UI_SPEC 4.5).
CHECK_LABELS = {
    "citation_validity": "Citations",
    "claim_support": "Claims",
    "number_date": "Numbers",
    "quote_limit": "Quotes",
}


class AnswerCitation(Citation):
    """Citation plus the source name shown on the source card (section 5)."""

    source_name: str


def source_name(url: str) -> str:
    """A readable source name from a corpus URL; Know Before You Fly is labelled a partner."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host == "govinfo.gov":
        return "14 CFR (GovInfo)" if "/CFR-" in parsed.path else "GovInfo"
    if host == "federalregister.gov":
        return "Federal Register"
    if host.endswith("knowbeforeyoufly.org"):
        return "Know Before You Fly (FAA partner campaign)"
    return host or "unknown source"


class _MeteredClient:
    """Passes calls to the LLM client and adds up the cost of every call for one answer."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.cost_usd: float | None = 0.0

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens=None) -> LLMResult:
        result = self.client.complete(system, messages, max_tokens=max_tokens)
        if self.cost_usd is not None:
            self.cost_usd = None if result.cost_usd is None else self.cost_usd + result.cost_usd
        return result


class _Stages:
    """Times each stage and reports it through on_stage."""

    def __init__(self, on_stage: OnStage | None) -> None:
        self.on_stage = on_stage
        self.ms: dict[str, float] = {name: 0.0 for name in STAGES}
        self._start: dict[str, float] = {}

    def _emit(self, name: str, status: str, ms: float) -> None:
        if self.on_stage is not None:
            self.on_stage(name, status, ms)

    def start(self, name: str) -> None:
        self._start[name] = time.perf_counter()
        self._emit(name, "started", 0.0)

    def end(self, name: str, status: str = "done") -> None:
        ms = round((time.perf_counter() - self._start.pop(name)) * 1000, 1)
        self.ms[name] = ms
        self._emit(name, status, ms)

    def skip(self, *names: str) -> None:
        for name in names:
            self._emit(name, "skipped", 0.0)


def _validate(cfg: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Split the config's checks between the two modules; reject config mistakes up front."""
    requested = list(cfg.get("checks") or [])
    known = set(answer_checks.CHECKS) | set(claim_check.CHECKS)
    unknown = [c for c in requested if c not in known]
    if unknown:
        raise ValueError(f"unknown check(s) {unknown}; known: {sorted(known)}")
    if cfg.get("gate") and cfg.get("gate_threshold") is not None and not cfg.get("rerank"):
        raise ValueError("the evidence gate uses rerank scores, so it needs rerank: true")
    return (
        [c for c in requested if c in answer_checks.CHECKS],
        [c for c in requested if c in claim_check.CHECKS],
    )


def _run_checks(
    sentences: list[Sentence],
    passages: Sequence[Mapping[str, Any]],
    basic: list[str],
    claims: list[str],
    client: Any,
    question: str | None = None,
) -> dict[str, Any]:
    """Both check modules over the answer; per-check pass flags and merged per-sentence verdicts.

    `question` is passed to the number check so a number the user gave is not treated
    as a claim the chunks must back.
    """
    requested = basic + claims
    chunk_texts = {p["chunk_id"]: p["text"] for p in passages}
    merged: list[dict[str, Any]] = [
        {"text": s["text"], "chunk_ids": s["chunk_ids"]} for s in sentences
    ]
    per_check: dict[str, bool] = {}
    if basic:
        res = answer_checks.check_sentences(sentences, chunk_texts, checks=basic)
        for m, v in zip(merged, res["sentences"], strict=True):
            m.update({name: v[name] for name in basic})
        for name in basic:
            per_check[name] = all(v[name]["ok"] for v in res["sentences"])
    if claims:
        judge = make_judge(client) if "claim_support" in claims else None
        res = claim_check.check_sentences(
            sentences, chunk_texts, judge=judge, checks=claims, question=question
        )
        for m, v in zip(merged, res["sentences"], strict=True):
            m.update({k: v[k] for k in (*claims, "error") if k in v})
        if "number_date" in claims:
            per_check["number_date"] = all(
                "error" not in v and v["number_date"]["ok"] for v in res["sentences"]
            )
        if "claim_support" in claims:
            per_check["claim_support"] = all(
                "error" not in v and v["claim_support"]["supported"] for v in res["sentences"]
            )
    for m in merged:
        m["ok"] = (
            all(
                m.get(name, {}).get("supported" if name == "claim_support" else "ok", False)
                for name in requested
            )
            and "error" not in m
        )
    return {
        "ok": bool(sentences) and all(per_check.values()),
        "requested": requested,
        "per_check": per_check,
        "sentences": merged,
    }


def _judge_error(checked: Mapping[str, Any]) -> str | None:
    """The first claim-judge error in the verdicts, if any."""
    for v in checked["sentences"]:
        reason = str((v.get("claim_support") or {}).get("reason", ""))
        if reason.startswith(JUDGE_ERROR_PREFIXES):
            return reason
    return None


def _failed_labels(verdict: Mapping[str, Any], requested: Sequence[str]) -> list[str]:
    """The checks one sentence failed, by their answer-card names."""
    failed = [
        CHECK_LABELS[n]
        for n in requested
        if not (verdict.get(n) or {}).get("supported" if n == "claim_support" else "ok", False)
    ]
    if "error" in verdict and "Citations" not in failed:
        failed.append("Citations")
    return failed


def _citations(
    sentences: list[Sentence], passages: Sequence[Mapping[str, Any]]
) -> list[AnswerCitation]:
    """One citation per cited chunk, numbered in order of first use."""
    by_id = {p["chunk_id"]: p for p in passages}
    citations: list[AnswerCitation] = []
    seen: set[str] = set()
    for s in sentences:
        for cid in s["chunk_ids"]:
            if cid in seen or cid not in by_id:
                continue
            seen.add(cid)
            p = by_id[cid]
            citations.append(
                {
                    "n": len(citations) + 1,
                    "chunk_id": cid,
                    "url": p["source_url"],
                    "title": p["title"],
                    "effective_date": p.get("effective_date"),
                    "source_name": source_name(p["source_url"]),
                }
            )
    return citations


def answer(
    question: str,
    history: Sequence[Mapping[str, str]],
    config: Mapping[str, Any],
    pilot_type: str | None = "not_sure",
    on_stage: OnStage | None = None,
    client: Any = None,
    retrieve_fn: Callable[..., list] | None = None,
    rerank_fn: Callable[..., list] | None = None,
) -> Answer:
    """Answer one question with the given run config (a dict from load_config()).

    history is a list of {"role": "user" | "assistant", "content": str} turns.
    client, retrieve_fn and rerank_fn default to the real LLM client, retrieve()
    and rerank(); tests and the server pass their own.
    """
    started = time.perf_counter()
    basic, claims = _validate(config)
    llm = _MeteredClient(client or LLMClient(config))
    stages = _Stages(on_stage)
    closed_book = bool(config.get("closed_book"))
    top_n = config.get("rerank_settings", {}).get("top_n", 5)

    trace: dict[str, Any] = {
        "candidates": [],
        "reranked": [],
        "gate_score": None,
        "checks": {},
        "refusal_reason": None,
        "stopped_at": None,
        "route_reasons": [],
    }

    def finish(status: str, sentences: list[Sentence], passages: Sequence[Mapping[str, Any]]):
        latency = dict(stages.ms)
        latency["total"] = round((time.perf_counter() - started) * 1000, 1)
        result: dict[str, Any] = {
            "status": status,
            "sentences": sentences if status == "answered" else [],
            "citations": _citations(sentences, passages) if status == "answered" else [],
            "referral": routed["referral"],
            "router_tag": routed["router_tag"],
            "rewritten_question": rewritten if rewritten != question else None,
            "trace": trace,
            "latency_ms": latency,
            "cost_usd": llm.cost_usd,
        }
        log_query(question, result, config_name=config.get("name"), pilot_type=pilot_type)
        return result

    def refuse(stage: str, reason: str, passages: Sequence[Mapping[str, Any]] = ()):
        trace["refusal_reason"] = reason
        trace["stopped_at"] = stage
        stages.end(stage, "stopped")
        remaining = STAGES[STAGES.index(stage) + 1 :]
        stages.skip(*remaining)
        return finish("refused", [], passages)

    # 1-2. Rewrite and route ("Understanding question").
    stages.start("route")
    rewritten = rewrite_followup(question, history, llm)
    routed = route(rewritten, pilot_type)
    trace["route_reasons"] = routed["reasons"]
    query = routed["question"]
    if not query.strip():
        return refuse("route", "nothing left to answer after removing injected instructions")
    stages.end("route")

    # 3. Retrieve and rerank.
    passages: list[dict[str, Any]] = []
    if closed_book:
        stages.skip("retrieve", "rerank")
    else:
        stages.start("retrieve")
        candidates = (retrieve_fn or _default_retrieve)(
            query,
            k=config.get("retrieval", {}).get("pool_size", 20),
            mode=config["retrieval_mode"],
            filters=None,
            strategy=config["chunk_strategy"],
        )
        trace["candidates"] = candidates
        stages.end("retrieve")

        if config.get("rerank"):
            stages.start("rerank")
            passages = list((rerank_fn or _default_rerank)(query, candidates, top_n=top_n))
            stages.end("rerank")
            if passages:
                trace["gate_score"] = max(p["rerank_score"] for p in passages)
        else:
            passages = list(candidates[:top_n])
            stages.skip("rerank")
        trace["reranked"] = passages

    # 4-5. Evidence gate, then generate ("Writing answer").
    stages.start("generate")
    threshold = config.get("gate_threshold")
    if config.get("gate") and threshold is not None and not closed_book:
        score = trace["gate_score"]
        if score is None or score < threshold:
            return refuse("generate", "evidence gate: no passage scored above the threshold")
    gen = generate(
        query,
        passages,
        config,
        llm,
        closed_book=closed_book,
        instructions=routed["instructions"],
    )
    trace["generation"] = {
        "status": gen["status"],
        "error": gen["error"],
        "raw_text": gen["raw_text"],
        "usage": gen["usage"],
    }
    if gen["status"] == "insufficient":
        return refuse("generate", "not enough support in the passages", passages)
    if gen["status"] == "error":
        return refuse("generate", f"generation error: {gen['error']}", passages)
    stages.end("generate")

    # 6-7. Checks, failing closed ("Checking safeguards").
    sentences = gen["sentences"]
    stages.start("check")
    if basic or claims:
        try:
            checked = _run_checks(sentences, passages, basic, claims, llm, question=query)
        except Exception as e:  # any check error means refuse (CLAUDE.md rule 7)
            trace["checks"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            return refuse("check", f"check error: {type(e).__name__}: {e}", passages)
        trace["checks"] = checked
        judge_error = _judge_error(checked)
        if judge_error:
            return refuse("check", f"check error: {judge_error}", passages)
        kept = [i for i, v in enumerate(checked["sentences"]) if v["ok"]]
        dropped = [i for i, v in enumerate(checked["sentences"]) if not v["ok"]]
        if dropped:
            # Keep the verdicts over every generated sentence for the trace and the log;
            # per_check then describes the answer that is shown.
            checked["per_check_all"] = dict(checked["per_check"])
            checked["dropped"] = [
                {
                    "index": i,
                    "text": sentences[i]["text"],
                    "failed": _failed_labels(checked["sentences"][i], checked["requested"]),
                }
                for i in dropped
            ]
            if not kept or len(dropped) > MAX_DROPPED_SENTENCES:
                failed = [CHECK_LABELS[n] for n, ok in checked["per_check"].items() if not ok]
                return refuse("check", f"check failed: {', '.join(failed)}", passages)
            sentences = [sentences[i] for i in kept]
            checked["per_check"] = dict.fromkeys(checked["per_check"], True)
            checked["ok"] = True
    else:
        trace["checks"] = {"ok": True, "requested": [], "per_check": {}, "sentences": []}
    stages.end("check")

    # 8-10. Referral, citations, latency, cost and the log are added in finish().
    return finish("answered", sentences, passages)


def _default_retrieve(*args, **kwargs):
    from src.retrieve.retrieve import retrieve

    return retrieve(*args, **kwargs)


def _default_rerank(*args, **kwargs):
    from src.retrieve.rerank import rerank

    return rerank(*args, **kwargs)
