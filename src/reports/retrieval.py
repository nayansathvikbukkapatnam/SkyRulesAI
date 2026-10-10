"""Fill the tables in reports/04_retrieval_reranking.md from reports/artifacts/ (rule 6).

Same convention as report 02: every number sits between

    <!-- BEGIN AUTO:<block> -->  ...  <!-- END AUTO:<block> -->

markers and is rewritten from the artifacts the make targets wrote. A missing
artifact renders as "not produced yet" with the command that makes it.

    python -m src.reports.retrieval                 # fill the report
    python -m src.reports.retrieval --demo-filter   # write filter_demo.json (needs the index)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from src.common import paths
from src.reports.chunking import _fmt, _load, _missing, _table

REPORT = paths.REPORTS / "04_retrieval_reranking.md"
# Retrieval-only configs: one switch at a time from B1/B2 to A3 (tests/test_config.py).
MODE_CONFIGS = ("b1", "b2", "a1", "a2", "a3")
NO_RERANK, RERANK = "a2", "a3"  # differ only in rerank
METRICS = (("Recall@5", "final", "recall@5"), ("Recall@20 (pool)", "pool", "recall@20"),
           ("MRR", "final", "mrr"), ("nDCG@10", "final", "ndcg@10"))  # fmt: skip

# Fixed demo queries for the page_type filter (not golden items; never scored).
FILTER_DEMO = (
    ("How much does it cost to register a drone?", "guide_page"),
    ("How much does it cost to register a drone?", "regulation_summary"),
    ("What are the operating limitations for small unmanned aircraft?", "toolkit_pdf"),
)


def _ci(ci: list[float] | None) -> str:
    return f"[{_fmt(ci[0])}, {_fmt(ci[1])}]" if ci else "–"


def _cell(run: dict, side: str, key: str) -> str:
    value = run[side][key]
    ci = (run.get("ci95") or {}).get(side, {}).get(key)
    return f"{_fmt(value)} {_ci(ci)}" if ci else _fmt(value)


def paired_diff_ci(
    before: dict, after: dict, side: str, key: str, n_resamples: int = 10_000, seed: int = 42
) -> tuple[float, list[float]] | None:
    """Mean per-item change (after - before) and its 95% paired bootstrap interval."""
    b = {r["id"]: r[side][key] for r in before["items"] if r.get(side)}
    a = {r["id"]: r[side][key] for r in after["items"] if r.get(side)}
    ids = sorted(b.keys() & a.keys())
    if not ids:
        return None
    diffs = np.array([a[i] - b[i] for i in ids], dtype=float)
    rng = np.random.default_rng(seed)
    means = diffs[rng.integers(0, len(diffs), size=(n_resamples, len(diffs)))].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return round(float(diffs.mean()), 4), [round(float(lo), 4), round(float(hi), 4)]


# --- Blocks ---------------------------------------------------------------------------


def block_modes(artifacts: Path) -> str:
    runs = {c: _load(artifacts, f"retrieval_{c}_dev.json") for c in MODE_CONFIGS}
    missing = [c for c, r in runs.items() if r is None]
    rows = []
    for config, run in runs.items():
        if run is None:
            rows.append([config.upper(), *["–"] * 7])
            continue
        s = run["switches"]
        rows.append([config.upper(), s["retrieval_mode"], f"`{s['chunk_strategy']}`",
                     "yes" if s["rerank"] else "no",
                     *[_cell(run, side, key) for _, side, key in METRICS]])  # fmt: skip
    table = _table(["Config", "Retrieval", "Chunks", "Rerank", *[m for m, _, _ in METRICS]], rows)
    n = next((r["n_scored"] for r in runs.values() if r), None)
    note = (
        f"\n\nDev split, {n} answerable items scored per config (unanswerable items have no "
        "evidence to find). Each cell is the mean with its 95% bootstrap interval "
        "(10,000 resamples over items, seed 42). Recall@20 is the 20-candidate pool; the other "
        "metrics are on the list the generator sees (the reranked top 5 for A3). Sources: "
        "`reports/artifacts/retrieval_<config>_dev.json`."
    )
    if missing:
        cmds = "`, `".join(f"make retrieval-eval CONFIG={c} SPLIT=dev" for c in missing)
        note += "\n\n" + _missing(f"dev retrieval metrics for {', '.join(missing)}", cmds)
    return table + note


def block_rerank(artifacts: Path) -> str:
    before = _load(artifacts, f"retrieval_{NO_RERANK}_dev.json")
    after = _load(artifacts, f"retrieval_{RERANK}_dev.json")
    if before is None or after is None:
        return _missing(
            f"dev retrieval metrics for {NO_RERANK} and {RERANK}",
            f"make retrieval-eval CONFIG={NO_RERANK} SPLIT=dev` and "
            f"`make retrieval-eval CONFIG={RERANK} SPLIT=dev",
        )
    changed = sorted(k for k in before["switches"] if before["switches"][k] != after["switches"][k])
    if changed != ["rerank"]:
        return f"_Cannot compare: {NO_RERANK} and {RERANK} differ in {changed}, not only rerank._"
    rows = []
    for label, side, key in METRICS[:1] + METRICS[2:]:  # Recall@20 of the pool cannot change
        diff = paired_diff_ci(before, after, side, key)
        rows.append([label, before[side][key], after[side][key],
                     f"{diff[0]:+.4f} {_ci(diff[1])}" if diff else "–"])  # fmt: skip
    lat = after["latency_ms"]["rerank"]
    return (
        _table(
            [
                "Metric (dev)",
                f"{NO_RERANK.upper()} (no rerank)",
                f"{RERANK.upper()} (rerank top 5)",
                "Change [95% paired CI]",
            ],
            rows,
        )  # fmt: skip
        + f"\n\n**Ceiling:** the reranker only reorders the pool, so Recall@20 of the pool, "
        f"{_fmt(after['pool_recall@20_ceiling'])}, is the most Recall@5 it can reach."
        + f"\n\n**Added latency (cross-encoder, CPU):** p50 {_fmt(lat['p50'])} ms, "
        f"p95 {_fmt(lat['p95'])} ms per query, over {after['n_scored']} dev items."
        + f"\n\nSources: `reports/artifacts/retrieval_{NO_RERANK}_dev.json`, "
        f"`retrieval_{RERANK}_dev.json`."
    )


def block_filter_demo(artifacts: Path) -> str:
    demo = _load(artifacts, "filter_demo.json")
    if not demo:
        return _missing(
            "page_type filter demonstration", "python -m src.reports.retrieval --demo-filter"
        )
    parts = []
    for case in demo["cases"]:
        rows = [[i + 1, f"`{r['page_type']}`", r["title"][:70]] for i, r in enumerate(case["top"])]
        ok = "all results match" if case["all_match"] else "**FILTER LEAKED**"
        parts.append(f"**{case['query']}**, filter `page_type = {case['page_type']}` ({ok}):\n\n"
                     + _table(["Rank", "page_type", "Title"], rows))  # fmt: skip
    return "\n\n".join(parts) + (
        f"\n\nHybrid retrieval on `{demo['strategy']}`, commit `{demo['git_commit']}`. Source: "
        "`reports/artifacts/filter_demo.json`."
    )


def block_gate(artifacts: Path) -> str:
    g = _load(artifacts, "gate_tuning_dev.json")
    if not g:
        return _missing("gate threshold tuning", "python -m src.eval.tune_gate --config a3")
    c, n = g["chosen"], g["no_gate"]
    rows = [[label, n[k], c[k]] for label, k in (("Correctness", "correctness"),
            ("Over-refusal", "over_refusal"), ("Correct refusal", "correct_refusal"))]  # fmt: skip
    return (
        f"Chosen `gate_threshold`: **{_fmt(c['threshold'])}** (rule: {g['rule']}), from "
        f"`{g['source']}` at commit `{g['source_git_commit']}`, {g['n_items']} dev items.\n\n"
        + _table(["Dev rate", "No gate", "Chosen gate"], rows)
        + "\n\nSource: `reports/artifacts/gate_tuning_dev.json`"
        + (
            " (curve: `gate_tuning_dev.png`)."
            if (artifacts / "gate_tuning_dev.png").exists()
            else "."
        )
    )


BLOCKS: dict[str, Callable[[Path], str]] = {
    "modes": block_modes,
    "rerank": block_rerank,
    "filter_demo": block_filter_demo,
    "gate": block_gate,
}
_BLOCK = re.compile(r"(<!-- BEGIN AUTO:(\w+) -->)\n(.*?)\n(<!-- END AUTO:\2 -->)", re.S)


def render(text: str, artifacts: Path) -> str:
    return _BLOCK.sub(
        lambda m: f"{m.group(1)}\n{BLOCKS[m.group(2)](artifacts)}\n{m.group(4)}", text
    )


# --- Filter demonstration ---------------------------------------------------------------


def demo_filter(retrieve: Callable[..., list], strategy: str = "struct@v1") -> dict[str, Any]:
    from src.eval.run_eval import git_commit

    cases = []
    for query, page_type in FILTER_DEMO:
        top = retrieve(
            query, k=5, mode="hybrid", filters={"page_type": page_type}, strategy=strategy
        )
        cases.append({
            "query": query,
            "page_type": page_type,
            "top": [{"chunk_id": c["chunk_id"], "page_type": c["page_type"], "title": c["title"]}
                    for c in top],
            "all_match": bool(top) and all(c["page_type"] == page_type for c in top),
        })  # fmt: skip
    return {"strategy": strategy, "git_commit": git_commit(), "cases": cases,
            "written_at": datetime.now(UTC).isoformat(timespec="seconds")}  # fmt: skip


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fill report 04's tables from artifacts.")
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--artifacts", type=Path, default=paths.ARTIFACTS)
    parser.add_argument("--demo-filter", action="store_true", help="write filter_demo.json first")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.demo_filter:
        from src.retrieve.retrieve import retrieve

        demo = demo_filter(retrieve)
        args.artifacts.mkdir(parents=True, exist_ok=True)
        (args.artifacts / "filter_demo.json").write_text(json.dumps(demo, indent=2) + "\n")
        leaks = [c["query"] for c in demo["cases"] if not c["all_match"]]
        print(f"filter demo: {len(demo['cases'])} cases, {len(leaks)} leaked")
    text = render(args.report.read_text(), args.artifacts)
    args.report.write_text(text)
    print(f"updated {args.report} ({text.count('_Not produced yet')} block(s) still waiting)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
