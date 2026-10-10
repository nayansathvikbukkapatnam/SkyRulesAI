"""Query log: one JSON line per answer() call in logs/queries.jsonl (gitignored).

Each record is built from an explicit list of fields, so nothing outside them
(and never a credential) can reach the file. It holds what report 05 and the
failure labels need: the question and rewrite, the router tag, candidate and
reranked chunk IDs with scores, check verdicts, latency per stage and cost.

Logging never breaks an answer: a write failure is reported as a warning.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.common import paths

log = logging.getLogger(__name__)

QUERY_LOG: Path = paths.QUERY_LOG
RAW_TEXT_LIMIT = 2000  # characters of the model's raw reply kept for debugging

_lock = threading.Lock()


def _candidate(c: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": c.get("chunk_id"),
        "dense_score": c.get("dense_score"),
        "bm25_score": c.get("bm25_score"),
        "fused_score": c.get("fused_score"),
    }


def _reranked(c: Mapping[str, Any]) -> dict[str, Any]:
    return {"chunk_id": c.get("chunk_id"), "rerank_score": c.get("rerank_score")}


def _checks(checks: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": checks.get("ok"),
        "requested": checks.get("requested", []),
        "per_check": checks.get("per_check", {}),
        "error": checks.get("error"),
        "sentences": [
            {"chunk_ids": s.get("chunk_ids", []), "ok": s.get("ok")}
            for s in checks.get("sentences", [])
        ],
    }


def _generation(gen: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not gen:
        return None
    return {
        "status": gen.get("status"),
        "error": gen.get("error"),
        "usage": gen.get("usage"),
        "raw_text": (gen.get("raw_text") or "")[:RAW_TEXT_LIMIT],
    }


def build_record(
    question: str,
    result: Mapping[str, Any],
    config_name: str | None = None,
    pilot_type: str | None = None,
) -> dict[str, Any]:
    """The log line for one answer, from named fields only."""
    trace = result.get("trace") or {}
    return {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "config": config_name,
        "pilot_type": pilot_type,
        "question": question,
        "rewritten_question": result.get("rewritten_question"),
        "router_tag": result.get("router_tag"),
        "route_reasons": trace.get("route_reasons", []),
        "status": result.get("status"),
        "refusal_reason": trace.get("refusal_reason"),
        "stopped_at": trace.get("stopped_at"),
        "sentences": [
            {"text": s.get("text"), "chunk_ids": s.get("chunk_ids", [])}
            for s in result.get("sentences", [])
        ],
        "citations": [c.get("chunk_id") for c in result.get("citations", [])],
        "referral_kind": (result.get("referral") or {}).get("kind"),
        "candidates": [_candidate(c) for c in trace.get("candidates", [])],
        "reranked": [_reranked(c) for c in trace.get("reranked", [])],
        "gate_score": trace.get("gate_score"),
        "checks": _checks(trace.get("checks") or {}),
        "generation": _generation(trace.get("generation")),
        "latency_ms": result.get("latency_ms"),
        "cost_usd": result.get("cost_usd"),
    }


def log_query(
    question: str,
    result: Mapping[str, Any],
    config_name: str | None = None,
    pilot_type: str | None = None,
    path: Path | None = None,
) -> None:
    """Append one line for this answer. Never raises."""
    target = Path(path or QUERY_LOG)
    try:
        line = json.dumps(
            build_record(question, result, config_name, pilot_type),
            ensure_ascii=False,
            default=str,
        )
        with _lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:  # the answer matters more than its log line
        log.warning("could not write the query log: %s: %s", type(e).__name__, e)
