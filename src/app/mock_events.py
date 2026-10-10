"""Build src/app/static/mock_events.json, the recorded events behind the page's ?mock=1 mode.

    python -m src.app.mock_events

The events come from the real answer() pipeline and the server's b3_check_flags(),
run over the labelled [FIXTURE] passages in tests/fixtures/candidates.json with a
scripted fake LLM. No network, no models, no key. Every passage title says
[FIXTURE], so mock answers can never be mistaken for real corpus content.
Stage timings are fixed so the file only changes when the contract does.
"""

from __future__ import annotations

import json
from typing import Any

from src.app.server import b3_check_flags
from src.common import paths
from src.common.config import load_config
from src.generate.answer import answer
from src.generate.claim_check import JUDGE_SYSTEM
from src.generate.llm import LLMResult, parse_json

OUT = paths.ROOT / "src" / "app" / "static" / "mock_events.json"
STAGE_MS = {"route": 42.0, "retrieve": 186.0, "rerank": 231.0, "generate": 1430.0, "check": 395.0}

CANDIDATES = json.loads((paths.FIXTURES / "candidates.json").read_text())["candidates"]
RULE = "In uncontrolled airspace, recreational flyers stay at or below 400 feet above ground level."


class ScriptedLLM:
    """Replies by system prompt: queued generation replies, and a fixed judge verdict."""

    def __init__(self, *generations: str, judge: str = "yes") -> None:
        self.generations = list(generations)
        self.judge = judge

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens=None) -> LLMResult:
        text = (
            json.dumps({"supported": self.judge, "reason": "Scripted verdict for the mock."})
            if system == JUDGE_SYSTEM
            else self.generations.pop(0)
        )
        return LLMResult(
            text=text,
            parsed_json=parse_json(text),
            input_tokens=980,
            output_tokens=48,
            latency_ms=900,
            cost_usd=0.000122,
            provider="anthropic",
            model_id="claude-haiku-5-5",
            stop_reason="end_turn",
            truncated=False,
            temperature=None,
            thinking="disabled",
            effort="medium",
            cached=False,
        )


def _retrieve(query, k=20, mode="hybrid", filters=None, strategy="struct@v1"):
    return [dict(c) for c in CANDIDATES]


def _rerank(query, candidates, top_n=5):
    order = [1, 0, 2, 3, 4, 5]  # the 400-foot passage first
    scores = [6.8, 3.1, 1.2, -0.4, -2.5, -4.0]
    return [{**candidates[i], "rerank_score": s} for i, s in zip(order, scores, strict=True)][
        :top_n
    ]


def _gen(*sentences: tuple[str, list[int]]) -> str:
    return json.dumps({"sentences": [{"text": t, "passages": p} for t, p in sentences]})


def _run(question: str, config: dict, llm: ScriptedLLM, name: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def on_stage(stage: str, status: str, ms: float) -> None:
        fixed = STAGE_MS[stage] if status in ("done", "stopped") else 0.0
        events.append(
            {
                "event": "stage",
                "data": {"name": stage, "status": status, "ms": fixed, "config": name},
            }
        )

    result = answer(
        question,
        [],
        config,
        on_stage=on_stage,
        client=llm,
        retrieve_fn=_retrieve,
        rerank_fn=_rerank,
    )
    result["latency_ms"] = {**STAGE_MS, "total": round(sum(STAGE_MS.values()), 1)}
    events.append({"event": "answer", "data": {**result, "config": name}})
    return events


def build() -> dict[str, Any]:
    b3, b2 = load_config("b3"), load_config("b2")
    for cfg in (b3, b2):
        cfg["name"] = "mock"  # keeps mock runs apart from real ones in the query log

    def scenario(question: str, b3_reply: str, b2_reply: str, b2_judge: str = "yes"):
        events = _run(question, b3, ScriptedLLM(b3_reply), "b3")
        b2_events = _run(question, b2, ScriptedLLM(b2_reply), "b2")
        b2_answer = b2_events[-1]["data"]
        b2_answer["b3_check_flags"] = b3_check_flags(b2_answer, b3, ScriptedLLM(judge=b2_judge))
        return events + b2_events + [{"event": "done", "data": {}}]

    return {
        "note": "Recorded with fixture passages and a scripted LLM. Not real answers.",
        "scenarios": {
            "answer": scenario(
                "How high can I fly as a hobby flyer?",
                _gen((RULE, [1])),
                _gen(
                    ("Recreational flyers can fly up to 500 feet.", [2]),
                    ("No permission is needed.", []),
                ),
            ),
            "location": scenario(
                "Can I fly in San Diego tonight at 10 pm?",
                _gen((RULE, [1])),
                _gen(("Yes, you can fly in San Diego tonight.", [])),
            ),
            "refusal": scenario(
                "Which drone should I buy?",
                '{"insufficient": true}',
                _gen(("A small drone is a good first choice.", [3])),
                b2_judge="no",
            ),
        },
    }


def main() -> None:
    try:  # once the query log (P9) is merged, keep mock runs out of logs/
        from src.generate import log as log_mod

        log_mod.QUERY_LOG = paths.ROOT / ".cache" / "mock_queries.jsonl"
    except ImportError:
        pass
    OUT.write_text(json.dumps(build(), indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {OUT.relative_to(paths.ROOT)}")


if __name__ == "__main__":
    main()
