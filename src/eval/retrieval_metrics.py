"""Retrieval metrics on the golden set: Recall@5, Recall@20, MRR and nDCG@10.

A retrieved chunk is a HIT for an evidence pair (source_url, evidence_quote) when
the chunk comes from that page (canonical URLs equal) and its normalized text
contains the normalized quote (normalize_text from golden_check: case, curly
quotes, dashes and whitespace only). Multi-page items have one pair per page:

- Recall@k: the share of an item's evidence pairs hit by the top k chunks
  (0 or 1 for single-page items);
- MRR: 1 / rank of the first hit, 0 if none;
- nDCG@10: binary gains, one per evidence pair (the first chunk to hit each pair),
  over an ideal ranking that hits every pair in the first ranks.

Items without evidence (unanswerable, and ambiguous ones with no source) are not
scored and are counted as skipped. A quote split across two chunks is a miss:
that is the "chunk boundary" failure the error analysis labels.

Each run scores two lists: `pool`, the top 20 from retrieve(), and `final`, what
the generator would see (the reranked top 5 when the config reranks, else the
pool). Recall@20 of the pool is the reranker's ceiling.

    make retrieval-eval CONFIG=b2 SPLIT=dev
    -> reports/artifacts/retrieval_<config>_<split>.json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.common import paths
from src.common.config import load_config, switches
from src.common.contracts import Candidate, canonical_url
from src.eval.golden_check import load_items, normalize_text

METRICS = ("recall@5", "recall@20", "mrr", "ndcg@10")

# A search returns (pool, final, latency_ms) for one question.
Search = Callable[[str], tuple[list[Candidate], list[Candidate], dict[str, float]]]


# --- Hits --------------------------------------------------------------------


def _as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def evidence_pairs(item: dict) -> list[tuple[str, str]]:
    """(canonical source_url, normalized quote) per cited page; [] if the item has none."""
    urls, quotes = _as_list(item.get("source_url")), _as_list(item.get("evidence_quote"))
    if not urls or len(urls) != len(quotes):
        return []
    return [(canonical_url(u), normalize_text(q)) for u, q in zip(urls, quotes, strict=True)]


def _searchable(text: str) -> tuple[str, str]:
    """The chunk text normalized, plus a copy without Markdown table pipes, so a quote
    copied from a table row matches (the same allowance golden_check makes)."""
    return normalize_text(text), normalize_text(re.sub(r"\s*\|\s*", " ", text))


def hits(candidate: Candidate, pairs: Sequence[tuple[str, str]]) -> set[int]:
    """Indices of the evidence pairs this chunk hits."""
    url = canonical_url(candidate["source_url"])
    texts = _searchable(candidate["text"])
    return {i for i, (u, q) in enumerate(pairs) if u == url and any(q in t for t in texts)}


# --- Per-item metrics --------------------------------------------------------


def item_metrics(ranked: Sequence[Candidate], pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Recall@5, Recall@20, MRR, nDCG@10 and the first hit's rank for one ranked list."""
    n = len(pairs)
    covered: set[int] = set()
    seen_at: dict[int, set[int]] = {}  # rank -> pairs first hit at that rank
    first_hit: int | None = None
    for rank, cand in enumerate(ranked, start=1):
        h = hits(cand, pairs)
        if h and first_hit is None:
            first_hit = rank
        new = h - covered
        if new:
            seen_at[rank] = new
            covered |= new

    def recall_at(k: int) -> float:
        return len(set().union(*(v for r, v in seen_at.items() if r <= k))) / n

    dcg = sum(len(v) / math.log2(r + 1) for r, v in seen_at.items() if r <= 10)
    idcg = sum(1 / math.log2(i + 1) for i in range(1, min(n, 10) + 1))
    return {
        "recall@5": recall_at(5),
        "recall@20": recall_at(20),
        "mrr": 1 / first_hit if first_hit else 0.0,
        # One chunk can hit two pairs at once (two quotes from one page), which the
        # one-pair-per-rank ideal does not model; that is still a perfect ranking.
        "ndcg@10": min(1.0, dcg / idcg),
        "first_hit_rank": first_hit,
    }


def summarize(rows: Sequence[dict[str, Any]], key: str) -> dict[str, float | None]:
    """Mean of each metric over the scored items, for one list ("pool" or "final")."""
    scored = [r[key] for r in rows if r.get(key)]
    return {
        m: (round(statistics.fmean(s[m] for s in scored), 4) if scored else None) for m in METRICS
    }


def bootstrap_ci(
    values: Sequence[float], n_resamples: int = 10_000, seed: int = 42, level: float = 0.95
) -> list[float] | None:
    """Percentile bootstrap interval for the mean, resampling items with replacement."""
    if not values:
        return None
    import numpy as np

    data = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    means = data[rng.integers(0, len(data), size=(n_resamples, len(data)))].mean(axis=1)
    tail = (1 - level) / 2 * 100
    lo, hi = np.percentile(means, [tail, 100 - tail])
    return [round(float(lo), 4), round(float(hi), 4)]


def intervals(rows: Sequence[dict[str, Any]], key: str) -> dict[str, list[float] | None]:
    """95% bootstrap interval of each metric's mean, for one list ("pool" or "final")."""
    scored = [r[key] for r in rows if r.get(key)]
    return {m: bootstrap_ci([s[m] for s in scored]) for m in METRICS}


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)], 1)


# --- Split guard ---------------------------------------------------------------


def frozen_tag_exists(tag: str = "b3-frozen") -> bool:
    out = subprocess.run(["git", "tag", "-l", tag], capture_output=True, text=True, cwd=paths.ROOT)
    return out.stdout.strip() == tag


def split_ids(split: str, split_file: Path, final: bool) -> set[str]:
    """Item IDs of one split. The test split runs once (CLAUDE.md rule 5): it needs
    --final and the b3-frozen tag."""
    if split not in ("dev", "test"):
        raise ValueError(f"split must be dev or test: {split!r}")
    if split == "test" and not (final and frozen_tag_exists()):
        raise SystemExit(
            "refusing SPLIT=test: it runs once, after the b3-frozen tag, with --final (rule 5)"
        )
    if not split_file.exists():
        raise FileNotFoundError(f"{split_file} not found; run make split first")
    ids = json.loads(split_file.read_text()).get(split)
    if not ids:
        raise ValueError(f"{split_file} has no {split!r} item IDs")
    return set(ids)


# --- Run -----------------------------------------------------------------------


def evaluate(items: Iterable[dict], search: Search) -> list[dict[str, Any]]:
    """Score every item. Items without evidence are kept, marked skipped."""
    rows = []
    for item in items:
        pairs = evidence_pairs(item)
        row: dict[str, Any] = {"id": item["id"], "category": item.get("category")}
        if not pairs:
            rows.append(row | {"skipped": "no evidence (unanswerable or no source)"})
            continue
        pool, final, latency = search(item["question"])
        rows.append(
            row
            | {
                "n_evidence": len(pairs),
                "pool": item_metrics(pool, pairs),
                "final": item_metrics(final, pairs),
                "pool_ids": [c["chunk_id"] for c in pool],
                "final_ids": [c["chunk_id"] for c in final],
                "latency_ms": latency,
            }
        )
    return rows


def report(cfg: dict, split: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in rows if "pool" in r]
    by_category: dict[str, Any] = {}
    for cat in sorted({r["category"] for r in scored if r["category"]}):
        subset = [r for r in scored if r["category"] == cat]
        by_category[cat] = {"n": len(subset), "final": summarize(subset, "final")}
    latency = {
        stage: {
            "p50": _percentile([r["latency_ms"][stage] for r in scored], 0.5),
            "p95": _percentile([r["latency_ms"][stage] for r in scored], 0.95),
        }
        for stage in ("retrieve", "rerank")
    }
    return {
        "config": cfg["name"],
        "split": split,
        "switches": switches(cfg),
        "n_items": len(rows),
        "n_scored": len(scored),
        "n_skipped": len(rows) - len(scored),
        "pool": summarize(scored, "pool"),
        "final": summarize(scored, "final"),
        "pool_recall@20_ceiling": summarize(scored, "pool")["recall@20"],
        "ci95": {"pool": intervals(scored, "pool"), "final": intervals(scored, "final")},
        "by_category": by_category,
        "latency_ms": latency,
        "items": rows,
        "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def build_search(cfg: dict) -> Search:
    """retrieve() with the config's mode and chunk strategy, then rerank if it is on."""
    if cfg["closed_book"]:
        raise SystemExit(f"config {cfg['name']!r} is closed-book: there is no retrieval to score")
    from src.retrieve.retrieve import Retriever

    retriever = Retriever(cfg)
    reranker = None
    if cfg["rerank"]:
        from src.retrieve.rerank import Reranker

        reranker = Reranker.from_config(cfg)
    pool_size = cfg["retrieval"]["pool_size"]
    top_n = cfg["rerank_settings"]["top_n"]

    def search(question: str):
        started = time.perf_counter()
        pool = retriever.retrieve(
            question, k=pool_size, mode=cfg["retrieval_mode"], strategy=cfg["chunk_strategy"]
        )
        latency = {"retrieve": (time.perf_counter() - started) * 1000, "rerank": 0.0}
        final = pool
        if reranker is not None:
            final = reranker.rerank(question, pool, top_n)
            latency["rerank"] = reranker.last_latency_ms
        return pool, final, latency

    return search


def main(argv: Iterable[str] | None = None, search: Search | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retrieval metrics on the golden set.")
    parser.add_argument("--config", required=True, help="b1 | b2 | a1 | a2 | a3 | b3")
    parser.add_argument("--split", default="dev")
    parser.add_argument("--final", action="store_true", help="the one test-split run (rule 5)")
    parser.add_argument("--golden", type=Path, default=paths.GOLDEN)
    parser.add_argument("--split-file", type=Path, default=paths.SPLIT)
    parser.add_argument("--artifacts", type=Path, default=paths.ARTIFACTS)
    args = parser.parse_args(list(argv) if argv is not None else None)

    cfg = load_config(args.config)
    ids = split_ids(args.split, args.split_file, args.final)
    items, errors = load_items(args.golden)
    if errors:
        raise ValueError(f"{args.golden}: {errors[0]} (run make golden-check)")
    items = [i for i in items if i.get("id") in ids]
    rows = evaluate(items, search or build_search(cfg))
    result = report(cfg, args.split, rows)

    args.artifacts.mkdir(parents=True, exist_ok=True)
    out = args.artifacts / f"retrieval_{args.config}_{args.split}.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    f = result["final"]
    print(
        f"{args.config} on {args.split}: {result['n_scored']} items scored "
        f"({result['n_skipped']} skipped); final R@5 {f['recall@5']}, MRR {f['mrr']}, "
        f"nDCG@10 {f['ndcg@10']}; pool R@20 {result['pool_recall@20_ceiling']}"
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
