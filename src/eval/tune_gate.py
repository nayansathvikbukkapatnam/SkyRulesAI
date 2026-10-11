"""Choose the evidence-gate threshold for a config from its dev-split eval run (S15).

    python -m src.eval.tune_gate --config b3
    -> reports/artifacts/gate_tuning_dev.json

The gate refuses before generation when the top rerank score is below the threshold.
Every dev item's top score is already in its trace (gate_score), so each candidate
threshold is replayed on the saved run without new LLM calls: an item the gate would
stop becomes a refusal (correct for unanswerable items, an over-refusal otherwise),
and every other item keeps its saved result.

Candidates are the midpoints of the gaps between the sorted dev scores, plus no gate.
The rule, in order: highest correctness; then fewest wrong answers (refusing beats a
wrong answer, CLAUDE.md rule 7); then fewest over-refusals; then the widest gap, so
the threshold sits as far as possible from the dev items on both sides.

Dev only: the test split is never read (CLAUDE.md rule 5).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.common import paths


def replay(items: list[dict], threshold: float | None) -> dict[str, Any]:
    """Run-level counts if the gate had used this threshold on the saved items."""
    correct = wrong = over = answerable = unanswerable = refused_right = 0
    gated = []
    for item in items:
        answer = item.get("answer") or {}
        score = (answer.get("trace") or {}).get("gate_score")
        stopped = threshold is not None and (score is None or score < threshold)
        is_unanswerable = item.get("category") == "unanswerable"
        refused = stopped or answer.get("status") != "answered"
        ok = is_unanswerable if stopped else bool(item["metrics"]["correct"])
        if stopped:
            gated.append(item["id"])
        correct += ok
        if is_unanswerable:
            unanswerable += 1
            refused_right += refused
        else:
            answerable += 1
            over += refused
            wrong += not refused and not ok
    n = len(items)
    return {
        "threshold": threshold,
        "correct": correct,
        "correctness": round(correct / n, 4) if n else None,
        "wrong_answers": wrong,
        "over_refusals": over,
        "over_refusal": round(over / answerable, 4) if answerable else None,
        "correct_refusal": round(refused_right / unanswerable, 4) if unanswerable else None,
        "gated": gated,
    }


def candidates(items: list[dict]) -> list[tuple[float | None, float]]:
    """(threshold, gap width): no gate, then the midpoint of each gap between scores."""
    scores = sorted(
        {
            s
            for i in items
            if (s := ((i.get("answer") or {}).get("trace") or {}).get("gate_score")) is not None
        }
    )
    out: list[tuple[float | None, float]] = [(None, float("inf"))]
    for low, high in zip(scores, scores[1:], strict=False):
        out.append((round((low + high) / 2, 2), high - low))
    return out


def choose(items: list[dict]) -> dict[str, Any]:
    rows = []
    for threshold, gap in candidates(items):
        row = replay(items, threshold)
        row["gap"] = None if gap == float("inf") else round(gap, 3)
        rows.append(row)

    def rank(row: dict) -> tuple:
        gap = float("inf") if row["gap"] is None else row["gap"]
        return (-row["correct"], row["wrong_answers"], row["over_refusals"], -gap)

    best = min(rows, key=rank)
    return {"chosen": best, "candidates": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default="b3")
    parser.add_argument("--artifacts", type=Path, default=paths.ARTIFACTS)
    args = parser.parse_args(argv)

    source = args.artifacts / f"eval_{args.config}_dev.json"
    run = json.loads(source.read_text())
    if run.get("split") != "dev":
        raise SystemExit(f"{source} is not a dev run; the gate is tuned on dev only")
    result = choose(run["items"])
    out = args.artifacts / "gate_tuning_dev.json"
    out.write_text(
        json.dumps(
            {
                "command": f"python -m src.eval.tune_gate --config {args.config}",
                "source": source.name,
                "source_commit": run.get("git_commit"),
                "rule": "max correctness, then fewest wrong answers, then fewest "
                "over-refusals, then the widest gap between dev scores",
                **result,
            },
            indent=2,
        )
        + "\n"
    )
    best = result["chosen"]
    print(
        f"gate_threshold {best['threshold']}: correctness {best['correctness']}, "
        f"wrong answers {best['wrong_answers']}, over-refusal {best['over_refusal']}, "
        f"gated {best['gated']}\nwrote {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
