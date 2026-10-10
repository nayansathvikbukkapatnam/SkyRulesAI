"""Fill the tables in reports/02_chunking.md from reports/artifacts/ (CLAUDE.md rule 6).

The report keeps its prose by hand. Every number sits between markers

    <!-- BEGIN AUTO:<block> -->
    ...
    <!-- END AUTO:<block> -->

and this script rewrites only what is between them, from the artifacts the make
targets wrote. A missing artifact renders as "not produced yet" with the command
that produces it, never as a typed number.

    make report-chunking
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from src.common import paths

REPORT = paths.REPORTS / "02_chunking.md"
TAGS = ("fixed@v1", "struct@v1")
# B2 -> A1 changes only the chunk strategy (tests/test_config.py enforces it).
BEFORE_CONFIG, AFTER_CONFIG = "b2", "a1"


def _load(artifacts: Path, name: str) -> Any | None:
    path = artifacts / name
    return json.loads(path.read_text()) if path.exists() else None


def _missing(what: str, command: str) -> str:
    return f"_Not produced yet: {what}. Run `{command}`._"


def _fmt(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") if value % 1 else f"{value:.0f}"
    return str(value)


def _table(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_fmt(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


# --- Blocks ---------------------------------------------------------------------------


def block_chunk_counts(artifacts: Path) -> str:
    stats = _load(artifacts, "chunk_stats.json") or {}
    rows = []
    for tag in TAGS:
        s = stats.get(tag)
        if not s:
            rows.append([f"`{tag}`", *["–"] * 9])
            continue
        t = s["n_tokens"]
        rows.append(
            [f"`{tag}`", s["n_chunks"], s["n_docs"], s["chunks_per_doc_median"], t["min"],
             t["median"], t["p95"], t["max"], t["mean"], s["n_over_max_tokens"]]
        )  # fmt: skip
    table = _table(
        ["Strategy", "Chunks", "Docs", "Chunks/doc (median)", "Min", "Median", "p95", "Max",
         "Mean", "Over 512"],
        rows,
    )  # fmt: skip
    missing = [t for t in TAGS if t not in stats]
    note = (
        "\n\n"
        + _missing(f"chunk stats for {', '.join(missing)}", "make chunk STRATEGY=fixed|struct")
        if missing
        else "\n\nToken counts use the bge-small tokenizer. "
        "Source: `reports/artifacts/chunk_stats.json`."
    )
    return table + note


def block_histograms(artifacts: Path) -> str:
    lines = []
    for tag in TAGS:
        png = artifacts / f"chunk_hist_{tag}.png"
        if png.exists():
            lines.append(f"![Chunk length, {tag}](artifacts/{png.name})")
        else:
            lines.append(_missing(f"histogram for {tag}", f"make chunk STRATEGY={tag[:-3]}"))
    return "\n\n".join(lines)


def block_page_types(artifacts: Path) -> str:
    stats = _load(artifacts, "chunk_stats.json") or {}
    if not any(t in stats for t in TAGS):
        return _missing("chunk stats", "make chunk STRATEGY=fixed|struct")
    types = sorted({p for t in TAGS for p in stats.get(t, {}).get("by_page_type", {})})
    rows = [[f"`{p}`", *[stats.get(t, {}).get("by_page_type", {}).get(p) for t in TAGS]]
            for p in types]  # fmt: skip
    return _table(["Page type", *[f"`{t}` chunks" for t in TAGS]], rows)


def block_embedding(artifacts: Path) -> str:
    info = _load(artifacts, "embedding_info.json")
    if not info:
        return _missing("embedding record", "make index STRATEGY=struct")
    rows = [
        ["Model", f"`{info['model']}`"],
        ["Hugging Face revision", f"`{info['hf_revision']}`"],
        ["Dimension", info["dimension"]],
        ["Distance (checked on the live collection)", info["distance"]],
        ["L2-normalized", info["normalized"]],
        ["Measured vector norms (sample)",
         f"{info['sample_norm_min']:.7f} to {info['sample_norm_max']:.7f}"],
        ["Query instruction",
         f"\"{info['query_instruction']}\"" if info["query_instruction"] else "none"],
        ["sentence-transformers / chromadb",
         f"{info['sentence_transformers']} / {info['chromadb']}"],
        ["Indexed strategy, chunks in collection", f"`{info['strategy']}`, {info['total']}"],
    ]  # fmt: skip
    return (
        _table(["Setting", "Value"], rows) + "\n\nSource: `reports/artifacts/embedding_info.json`."
    )


def block_migration(artifacts: Path) -> str:
    m = _load(artifacts, "migration.json")
    if not m:
        return _missing("migration record", "make migrate FROM=fixed@v1 TO=struct@v1")
    c = m["orphan_check"]
    rows = [
        ["Normalized documents read (no re-crawl)", m["n_docs"]],
        ["Index before", ", ".join(f"`{k}`: {v}" for k, v in m["before"].items()) or "empty"],
        [f"`{m['to']}` chunks generated", m["generated"]],
        [f"`{m['from']}` chunks deleted (metadata filter)", m["deleted_old"]],
        [f"Stale `{m['to']}` chunks deleted", m["deleted_stale_new"]],
        [f"`{m['to']}` chunks upserted", m["upserted"]],
        ["Index after", ", ".join(f"`{k}`: {v}" for k, v in m["after"].items()) or "empty"],
        ["Index IDs − manifest IDs", c["index_minus_manifest"]],
        ["Manifest IDs − index IDs", c["manifest_minus_index"]],
        [f"Chunks still tagged `{m['from']}`", c["old_strategy_count"]],
        ["Orphan check", "**PASS**" if c["passed"] else "**FAIL**"],
        ["Commit, run at", f"`{m['commit']}`, {m['run_at']}"],
    ]
    return (
        _table(["Step", "Result"], rows)
        + "\n\nSources: `reports/artifacts/migration.json`, full log in "
        "`reports/artifacts/migration_log.txt`."
    )


METRIC_ROWS = (
    ("Recall@5", "final", "recall@5"),
    ("Recall@20 (pool)", "pool", "recall@20"),
    ("MRR", "final", "mrr"),
    ("nDCG@10", "final", "ndcg@10"),
)


def block_dev_metrics(artifacts: Path) -> str:
    before = _load(artifacts, f"retrieval_{BEFORE_CONFIG}_dev.json")
    after = _load(artifacts, f"retrieval_{AFTER_CONFIG}_dev.json")
    missing = [c for c, r in ((BEFORE_CONFIG, before), (AFTER_CONFIG, after)) if r is None]
    if missing:
        commands = "` and `".join(f"make retrieval-eval CONFIG={c} SPLIT=dev" for c in missing)
        return _missing(f"dev retrieval metrics for {', '.join(missing)}", commands)
    changed = sorted(k for k in before["switches"] if before["switches"][k] != after["switches"][k])
    if changed != ["chunk_strategy"]:
        return (
            f"_Cannot compare: {BEFORE_CONFIG} and {AFTER_CONFIG} differ in {changed}, "
            "not only the chunk strategy._"
        )
    rows = []
    for label, side, key in METRIC_ROWS:
        b, a = before[side][key], after[side][key]
        delta = round(a - b, 4) if a is not None and b is not None else None
        rows.append([label, b, a, f"{delta:+.4f}" if delta is not None else "–"])
    header = [
        "Metric (dev split)",
        f"{BEFORE_CONFIG.upper()} (`{before['switches']['chunk_strategy']}`)",
        f"{AFTER_CONFIG.upper()} (`{after['switches']['chunk_strategy']}`)",
        "Change",
    ]
    return (
        _table(header, rows)
        + f"\n\nItems scored: {before['n_scored']} ({BEFORE_CONFIG}), {after['n_scored']} "
        f"({AFTER_CONFIG}); unanswerable items are not scored. Both runs are dense retrieval "
        "with no reranker, so the only difference is the chunk strategy. Sources: "
        f"`reports/artifacts/retrieval_{BEFORE_CONFIG}_dev.json`, "
        f"`retrieval_{AFTER_CONFIG}_dev.json`. Confidence intervals: report 04 and 06."
    )


BLOCKS: dict[str, Callable[[Path], str]] = {
    "chunk_counts": block_chunk_counts,
    "histograms": block_histograms,
    "page_types": block_page_types,
    "embedding": block_embedding,
    "migration": block_migration,
    "dev_metrics": block_dev_metrics,
}

_BLOCK = re.compile(r"(<!-- BEGIN AUTO:(\w+) -->)\n(.*?)\n?(<!-- END AUTO:\2 -->)", re.S)


def render(text: str, artifacts: Path) -> str:
    """Replace every AUTO block's body. Unknown or unclosed blocks are an error."""
    seen: list[str] = []

    def fill(m: re.Match) -> str:
        name = m.group(2)
        if name not in BLOCKS:
            raise ValueError(f"unknown AUTO block {name!r}; known: {sorted(BLOCKS)}")
        seen.append(name)
        return f"{m.group(1)}\n{BLOCKS[name](artifacts)}\n{m.group(4)}"

    out = _BLOCK.sub(fill, text)
    opened = re.findall(r"<!-- BEGIN AUTO:(\w+) -->", text)
    if sorted(opened) != sorted(seen):
        raise ValueError(f"unclosed AUTO block(s): {sorted(set(opened) - set(seen))}")
    return out


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fill report 02's tables from artifacts.")
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--artifacts", type=Path, default=paths.ARTIFACTS)
    args = parser.parse_args(list(argv) if argv is not None else None)

    text = args.report.read_text()
    args.report.write_text(render(text, args.artifacts))
    pending = render(text, args.artifacts).count("_Not produced yet")
    print(f"updated {args.report} ({pending} block(s) still waiting for artifacts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
