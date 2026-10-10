"""Choose B3's evidence-gate threshold on the dev split, and record how it was chosen.

The gate refuses when the best reranked passage scores below `gate_threshold`.
answer() records that best score as `trace.gate_score` whenever reranking is on,
even with the gate off, so one dev run of a reranking config without a gate (A3 by
default) is enough to replay every threshold offline, with no extra LLM calls:

- an item whose gate_score is below the threshold becomes a refusal;
- answerable items: correct only if they were correct and not gated;
- unanswerable items: correct if refused, by the gate or by the original run.

The chosen threshold maximizes correctness on dev; ties go to the lower
over-refusal rate, then to the lower threshold (the gentler gate). Candidates are
the midpoints between neighbouring observed gate scores, so every distinct outcome
is tried and the chosen value sits in the middle of its gap.

    python -m src.eval.tune_gate --config a3
    -> reports/artifacts/gate_tuning_dev.json and gate_tuning_dev.png

Dev only (CLAUDE.md rule 5): it refuses any eval file that is not a dev run.
Setting configs/b3.yaml's gate_threshold is a separate shared-file PR.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.common import paths


def _gate_score(record: dict) -> float | None:
    answer = record.get("answer") or {}
    score = (answer.get("trace") or {}).get("gate_score")
    return None if score is None or (isinstance(score, float) and math.isnan(score)) else score


def simulate(records: Sequence[dict], threshold: float) -> dict[str, Any]:
    """Outcomes if the gate refused every item whose gate_score < threshold."""
    correct = over = refused_right = n_ans = n_unans = gated = 0
    for r in records:
        m = r["metrics"]
        score = _gate_score(r)
        # No score means nothing was retrieved: the real gate would also refuse.
        blocked = score is None or score < threshold
        answered = m["answered"] and not blocked
        gated += int(m["answered"] and blocked)
        if r["category"] == "unanswerable":
            n_unans += 1
            refused_right += int(not answered)
            correct += int(not answered)
        else:
            n_ans += 1
            over += int(not answered)
            correct += int(answered and bool(m["correct"]))
    n = n_ans + n_unans
    return {
        "threshold": threshold,
        "correctness": round(correct / n, 4) if n else None,
        "over_refusal": round(over / n_ans, 4) if n_ans else None,
        "correct_refusal": round(refused_right / n_unans, 4) if n_unans else None,
        "newly_gated": gated,
    }


def tune(records: Sequence[dict]) -> dict[str, Any]:
    if any(r["metrics"]["correct"] is None for r in records if r["category"] != "unanswerable"):
        raise ValueError("the eval run has ungraded items (run with the judge, not --no-judge)")
    scores = sorted({s for r in records if (s := _gate_score(r)) is not None})
    if not scores:
        raise ValueError("no gate_score in the eval file: the config must rerank (e.g. a3)")
    # Outcomes only change at observed scores, so try the midpoint between each pair of
    # neighbours: on dev it behaves like any threshold in that gap, and it leaves the most
    # room on both sides for unseen questions.
    mids = [round((a + b) / 2, 4) for a, b in zip(scores, scores[1:], strict=False)]
    candidates = [-math.inf, *mids, math.inf]
    curve = [simulate(records, t) for t in candidates]
    best = min(curve, key=lambda c: (-c["correctness"], c["over_refusal"] or 0, c["threshold"]))
    baseline = curve[0]  # -inf: the gate never fires
    return {"chosen": best, "no_gate": baseline, "curve": curve}


def plot(curve: Sequence[dict], chosen: float, out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    finite = [c for c in curve if math.isfinite(c["threshold"])]
    xs = [c["threshold"] for c in finite]
    fig, ax = plt.subplots(figsize=(7, 4))
    for key, color in (("correctness", "#3b6ea5"), ("over_refusal", "#a33"),
                       ("correct_refusal", "#3a8a4f")):  # fmt: skip
        ax.plot(xs, [c[key] for c in finite], label=key.replace("_", " "), color=color)
    ax.axvline(chosen, color="#555", linestyle="--", linewidth=1, label=f"chosen {chosen:.2f}")
    ax.set_xlabel("gate threshold (top rerank score)")
    ax.set_ylabel("rate on dev")
    ax.set_title("Evidence gate threshold sweep (dev split)")
    ax.legend()
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=120)
    plt.close(fig)


def _jsonable(obj: Any) -> Any:
    """JSON has no infinity: write the never-gate and always-gate ends as strings."""
    if isinstance(obj, float) and math.isinf(obj):
        return "inf" if obj > 0 else "-inf"
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    return obj


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tune the evidence-gate threshold on dev.")
    parser.add_argument("--config", default="a3", help="a reranking config without a gate")
    parser.add_argument("--artifacts", type=Path, default=paths.ARTIFACTS)
    args = parser.parse_args(list(argv) if argv is not None else None)

    source = args.artifacts / f"eval_{args.config}_dev.json"
    if not source.exists():
        raise FileNotFoundError(f"{source} not found; run make eval CONFIG={args.config} SPLIT=dev")
    run = json.loads(source.read_text())
    if run.get("split") != "dev":
        raise SystemExit(f"{source} is not a dev run; the gate is tuned on dev only (rule 5)")
    if run["switches"].get("gate"):
        raise SystemExit(f"{args.config} already gates; tune on a reranking config without one")

    result = tune(run["items"])
    chosen = result["chosen"]
    record = {
        "source": source.name,
        "source_git_commit": run.get("git_commit"),
        "n_items": len(run["items"]),
        "rule": "maximize dev correctness; ties: lower over-refusal, then lower threshold",
        **result,
        "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    out = args.artifacts / "gate_tuning_dev.json"
    out.write_text(json.dumps(_jsonable(record), indent=2) + "\n")
    if math.isfinite(chosen["threshold"]):
        plot(result["curve"], chosen["threshold"], args.artifacts / "gate_tuning_dev.png")
    print(
        f"chosen gate_threshold {chosen['threshold']}: correctness {chosen['correctness']} "
        f"(no gate {result['no_gate']['correctness']}), over-refusal {chosen['over_refusal']}, "
        f"correct refusal {chosen['correct_refusal']}"
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
