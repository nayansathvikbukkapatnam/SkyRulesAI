"""Confidence intervals and paired tests over saved eval runs (task A6, DELIVERABLES section 4).

Reads reports/artifacts/eval_<config>_<split>.json (written by src/eval/run_eval.py) and
writes reports/artifacts/stats_<split>.json:

- per config: 95% bootstrap intervals (10,000 resamples over items, seed 42) for
  correctness, citation validity, correct refusal and over-refusal. Items where a
  metric is None (not applicable or not graded) are left out, and n is reported;
- paired comparisons along the ablation B2 -> A1 -> A2 -> A3 -> B3, plus B0 vs B3 and
  B2 vs B3: the change in correctness with a paired bootstrap interval, and McNemar's
  exact test on per-item correct / incorrect, with its 2x2 table. Items are paired by
  "id" and kept only when both runs graded them;
- B0 leakage: the items B0 (closed book) got wrong, and every config's correctness on
  that subset, so a gain cannot be just the model's prior knowledge.

Every number comes from the eval files; a missing file is reported as missing. With
--report it also fills the AUTO tables in reports/06_final_performance.md.

    make stats                     # dev split (default)
    make stats SPLIT=test FINAL=1  # once, after the b3-frozen tag (CLAUDE.md rule 5)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from statsmodels.stats.contingency_tables import mcnemar

from src.common import paths
from src.eval.retrieval_metrics import frozen_tag_exists

CONFIGS = ("b0", "b1", "b2", "a1", "a2", "a3", "b3")
ABLATION = ("b2", "a1", "a2", "a3", "b3")
EXTRA_PAIRS = (("b0", "b3"), ("b2", "b3"))
METRICS = {
    "correctness": "correct",
    "citation_validity": "citation_valid",
    "correct_refusal": "refused_correctly",
    "over_refusal": "over_refused",
}
N_RESAMPLES = 10_000
SEED = 42
CONFIDENCE = 0.95
REPORT = paths.REPORTS / "06_final_performance.md"


# --- Loading ---------------------------------------------------------------------------


def load_runs(artifacts: Path, split: str) -> dict[str, dict]:
    """The eval runs that exist for a split, by config name."""
    runs = {}
    for config in CONFIGS:
        path = artifacts / f"eval_{config}_{split}.json"
        if path.exists():
            runs[config] = json.loads(path.read_text(encoding="utf-8"))
    return runs


def by_id(run: dict) -> dict[str, dict]:
    return {item["id"]: item for item in run["items"]}


def graded(run: dict, key: str) -> list[bool]:
    """A metric's per-item values, without the items where it is None."""
    values = [item["metrics"].get(key) for item in run["items"]]
    return [bool(v) for v in values if v is not None]


# --- Statistics ------------------------------------------------------------------------


def _percentiles(samples: np.ndarray) -> tuple[float, float]:
    tail = (1 - CONFIDENCE) / 2 * 100
    low, high = np.percentile(samples, [tail, 100 - tail])
    return float(low), float(high)


def bootstrap_rate(values: list[bool], seed: int = SEED) -> dict[str, Any]:
    """Rate with a percentile bootstrap interval, resampling items with replacement."""
    n = len(values)
    if n == 0:
        return {"rate": None, "low": None, "high": None, "n": 0}
    data = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    means = data[rng.integers(0, n, size=(N_RESAMPLES, n))].mean(axis=1)
    low, high = _percentiles(means)
    return {
        "rate": round(float(data.mean()), 4),
        "low": round(low, 4),
        "high": round(high, 4),
        "n": n,
    }


def paired_comparison(before: dict, after: dict, seed: int = SEED) -> dict[str, Any]:
    """Change in correctness from `before` to `after` on the items both graded."""
    a_items, b_items = by_id(before), by_id(after)
    ids = sorted(
        i
        for i in a_items.keys() & b_items.keys()
        if a_items[i]["metrics"].get("correct") is not None
        and b_items[i]["metrics"].get("correct") is not None
    )
    n = len(ids)
    if n == 0:
        return {"n": 0, "difference": None, "low": None, "high": None, "table": None, "p": None}
    a = np.array([bool(a_items[i]["metrics"]["correct"]) for i in ids], dtype=float)
    b = np.array([bool(b_items[i]["metrics"]["correct"]) for i in ids], dtype=float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(N_RESAMPLES, n))
    low, high = _percentiles(b[idx].mean(axis=1) - a[idx].mean(axis=1))
    both = int(((a == 1) & (b == 1)).sum())
    only_before = int(((a == 1) & (b == 0)).sum())
    only_after = int(((a == 0) & (b == 1)).sum())
    neither = int(((a == 0) & (b == 0)).sum())
    table = [[both, only_before], [only_after, neither]]
    # With no discordant items the two runs agree everywhere: no evidence of a difference.
    p = 1.0 if only_before + only_after == 0 else float(mcnemar(table, exact=True).pvalue)
    return {
        "n": n,
        "before_rate": round(float(a.mean()), 4),
        "after_rate": round(float(b.mean()), 4),
        "difference": round(float(b.mean() - a.mean()), 4),
        "low": round(low, 4),
        "high": round(high, 4),
        "table": table,
        "table_layout": "[[both correct, only before correct], [only after correct, neither]]",
        "p": round(p, 6),
    }


def b0_incorrect_subset(runs: dict[str, dict]) -> dict[str, Any] | None:
    """Items closed-book B0 got wrong, and every config's correctness on them."""
    if "b0" not in runs:
        return None
    wrong = sorted(
        item["id"] for item in runs["b0"]["items"] if item["metrics"].get("correct") is False
    )
    subset = set(wrong)
    per_config = {}
    for config, run in runs.items():
        values = [
            bool(item["metrics"]["correct"])
            for item in run["items"]
            if item["id"] in subset and item["metrics"].get("correct") is not None
        ]
        per_config[config] = bootstrap_rate(values)
    return {"ids": wrong, "n": len(wrong), "correctness": per_config}


def comparisons(runs: dict[str, dict]) -> list[dict[str, Any]]:
    pairs = list(zip(ABLATION, ABLATION[1:], strict=False)) + list(EXTRA_PAIRS)
    out = []
    for before, after in pairs:
        entry: dict[str, Any] = {"before": before, "after": after}
        missing = [c for c in (before, after) if c not in runs]
        if missing:
            entry["missing"] = missing
        else:
            entry.update(paired_comparison(runs[before], runs[after]))
        out.append(entry)
    return out


def compute(runs: dict[str, dict], split: str) -> dict[str, Any]:
    per_config = {}
    for config, run in runs.items():
        per_config[config] = {
            name: bootstrap_rate(graded(run, key)) for name, key in METRICS.items()
        }
        summary = run.get("summary") or {}
        per_config[config]["latency_ms_total"] = (summary.get("latency_ms") or {}).get("total")
        per_config[config]["cost_usd_per_query"] = summary.get("cost_usd_per_query")
        per_config[config]["n_items"] = len(run["items"])
        per_config[config]["n_errors"] = sum(1 for i in run["items"] if i.get("error"))
    return {
        "command": f"make stats SPLIT={split}",
        "split": split,
        "computed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "method": {
            "interval": f"percentile bootstrap, {int(CONFIDENCE * 100)}%, resampling items",
            "resamples": N_RESAMPLES,
            "seed": SEED,
            "paired_test": "McNemar exact (statsmodels), per-item correct / incorrect",
            "pairing": "by item id; only items both runs graded",
            "none_values": "left out of each metric; n reported",
        },
        "inputs": {
            config: {
                "file": f"reports/artifacts/eval_{config}_{split}.json",
                "git_commit": run.get("git_commit"),
                "switches": run.get("switches"),
            }
            for config, run in runs.items()
        },
        "missing_configs": [c for c in CONFIGS if c not in runs],
        "per_config": per_config,
        "comparisons": comparisons(runs),
        "b0_incorrect_subset": b0_incorrect_subset(runs),
    }


# --- Report 06 tables ------------------------------------------------------------------


def _ci(r: dict | None) -> str:
    if not r or r.get("rate") is None:
        return "-"
    return f"{r['rate']:.3f} [{r['low']:.3f}, {r['high']:.3f}] (n={r['n']})"


def _table(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return "\n".join(lines + ["| " + " | ".join(str(c) for c in row) + " |" for row in rows])


def _missing(what: str, command: str) -> str:
    return f"_Not produced yet: {what}. Run `{command}`._"


def block_run_info(stats: dict | None) -> str:
    if not stats:
        return _missing("stats", "make stats")
    rows = [
        [config.upper(), f"`{i['file']}`", f"`{i['git_commit']}`"]
        for config, i in stats["inputs"].items()
    ]
    return (
        f"Split: **{stats['split']}**. Statistics computed {stats['computed_at']} "
        f"by `{stats['command']}`.\n\n" + _table(["Config", "Eval file", "Commit"], rows)
    )


def block_baselines(stats: dict | None) -> str:
    if not stats or not stats["per_config"]:
        return _missing("eval runs", "make eval CONFIG=<config> SPLIT=<split>, then make stats")
    rows = []
    for config in CONFIGS:
        s = stats["per_config"].get(config)
        if not s:
            rows.append([config.upper(), "not run", "-", "-", "-", "-", "-"])
            continue
        latency = s["latency_ms_total"] or {}
        rows.append(
            [config.upper(), _ci(s["correctness"]), _ci(s["citation_validity"]),
             _ci(s["correct_refusal"]), _ci(s["over_refusal"]),
             f"{latency.get('p50', '-')} / {latency.get('p95', '-')}",
             s["cost_usd_per_query"] if s["cost_usd_per_query"] is not None else "-"]
        )  # fmt: skip
    header = ["Config", "Correctness", "Citation validity", "Correct refusal", "Over-refusal",
              "Latency p50 / p95 (ms)", "Cost per query (USD)"]  # fmt: skip
    source = f"reports/artifacts/stats_{stats['split']}.json"
    return _table(header, rows) + f"\n\nRates with 95% bootstrap intervals. Source: `{source}`."


def block_ablation(stats: dict | None) -> str:
    if not stats:
        return _missing("stats", "make stats")
    rows = []
    for c in stats["comparisons"]:
        name = f"{c['before'].upper()} -> {c['after'].upper()}"
        if c.get("missing"):
            rows.append([name, "missing: " + ", ".join(c["missing"]), "-", "-", "-"])
        elif c["n"] == 0:
            rows.append([name, "no items graded by both", "-", "-", "-"])
        else:
            t = c["table"]
            rows.append(
                [name, f"{c['before_rate']:.3f} -> {c['after_rate']:.3f} (n={c['n']})",
                 f"{c['difference']:+.3f} [{c['low']:+.3f}, {c['high']:+.3f}]",
                 f"{t[0][1]} / {t[1][0]}", f"{c['p']:.4f}"]
            )  # fmt: skip
    header = ["Change", "Correctness", "Difference [95% CI]", "Only before / only after correct",
              "McNemar p (exact)"]  # fmt: skip
    note = "An interval that contains 0, or p >= 0.05, means the change is not shown to matter."
    return _table(header, rows) + f"\n\n{note}"


def block_leakage(stats: dict | None) -> str:
    if not stats:
        return _missing("stats", "make stats")
    leak = stats["b0_incorrect_subset"]
    if leak is None:
        return _missing("the B0 run", "make eval CONFIG=b0 SPLIT=<split>, then make stats")
    rows = [[c.upper(), _ci(r)] for c, r in leak["correctness"].items()]
    return f"Items closed-book B0 got wrong: {leak['n']}.\n\n" + _table(
        ["Config", "Correctness on the B0-incorrect subset"], rows
    )


def block_failures(split: str, artifacts: Path) -> str:
    path = artifacts / f"failure_labels_{split}.json"
    if not path.exists():
        return _missing(
            "failure labels", f"python -m src.eval.failure_labels count --split {split}"
        )
    f = json.loads(path.read_text(encoding="utf-8"))
    rows = [[f"`{label}`", n] for label, n in f["counts"].items()]
    rows.append(["**Total labelled**", f["total"]])
    return _table(["Failure category", "Count"], rows) + (
        f"\n\nLabelled by {', '.join(f['labelled_by'])}, from the "
        f"{', '.join(c.upper() for c in f['configs'])} run(s). "
        f"Source: `reports/artifacts/failure_labels_{split}.json`."
    )


_BLOCK = re.compile(r"(<!-- BEGIN AUTO:(\w+) -->)\n(.*?)\n?(<!-- END AUTO:\2 -->)", re.S)


def render_report(text: str, stats: dict | None, split: str, artifacts: Path) -> str:
    blocks = {
        "run_info": lambda: block_run_info(stats),
        "baselines": lambda: block_baselines(stats),
        "ablation": lambda: block_ablation(stats),
        "leakage": lambda: block_leakage(stats),
        "failures": lambda: block_failures(split, artifacts),
    }

    def fill(m: re.Match) -> str:
        if m.group(2) not in blocks:
            raise ValueError(f"unknown AUTO block {m.group(2)!r}")
        return f"{m.group(1)}\n{blocks[m.group(2)]()}\n{m.group(4)}"

    return _BLOCK.sub(fill, text)


# --- CLI -------------------------------------------------------------------------------


def check_split(split: str, final: bool) -> None:
    """The test split runs once (CLAUDE.md rule 5): only with --final after b3-frozen."""
    if split not in ("dev", "test"):
        raise SystemExit(f"split must be dev or test: {split!r}")
    if split == "test" and not (final and frozen_tag_exists()):
        raise SystemExit(
            "refusing SPLIT=test: the test split runs once, after the b3-frozen tag, "
            "with FINAL=1 (CLAUDE.md rule 5)"
        )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap intervals and McNemar tests.")
    # make passes command-line variables (SPLIT=..., FINAL=1) to the recipe's environment.
    parser.add_argument("--split", default=os.environ.get("SPLIT", "dev"))
    parser.add_argument("--final", action="store_true", default=os.environ.get("FINAL") == "1")
    parser.add_argument("--artifacts", type=Path, default=paths.ARTIFACTS)
    parser.add_argument("--report", action="store_true", help="also fill report 06's tables")
    parser.add_argument("--report-path", type=Path, default=REPORT)
    args = parser.parse_args(list(argv) if argv is not None else None)

    check_split(args.split, args.final)
    runs = load_runs(args.artifacts, args.split)
    if not runs:
        print(f"no eval runs for split {args.split!r} in {args.artifacts}; run make eval first")
        return 1
    stats = compute(runs, args.split)
    out = args.artifacts / f"stats_{args.split}.json"
    out.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {out} from {', '.join(runs)}; "
        f"missing: {', '.join(stats['missing_configs']) or 'none'}"
    )
    if args.report and args.report_path.exists():
        text = args.report_path.read_text(encoding="utf-8")
        args.report_path.write_text(
            render_report(text, stats, args.split, args.artifacts), encoding="utf-8"
        )
        print(f"updated {args.report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
