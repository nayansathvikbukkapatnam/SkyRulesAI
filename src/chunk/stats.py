"""Chunk statistics for report 02: counts and token-length distributions per strategy.

Reads a chunk file and writes, under reports/artifacts/:
- chunk_stats.json: one entry per strategy tag (other tags already in the file are
  kept, so fixed@v1 and struct@v1 sit side by side for the comparison);
- chunk_hist_<tag>.png: a histogram of n_tokens.

    make chunk STRATEGY=fixed   (runs this after chunking)
    python -m src.chunk.stats --strategy struct
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from src.common import paths
from src.common.contracts import Chunk

STATS_FILE = "chunk_stats.json"


def read_chunk_file(path: Path) -> list[Chunk]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run make chunk STRATEGY=... first")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def chunk_stats(chunks: Sequence[Chunk], max_tokens: int = 512) -> dict[str, Any]:
    """Counts and the n_tokens distribution (min, median, p95, max, mean)."""
    lengths = np.array([c["n_tokens"] for c in chunks], dtype=float)
    per_doc = Counter(c["doc_id"] for c in chunks)
    tokens: dict[str, Any] = {k: None for k in ("min", "median", "p95", "max", "mean")}
    if lengths.size:
        tokens = {
            "min": int(lengths.min()),
            "median": float(np.median(lengths)),
            "p95": float(np.percentile(lengths, 95)),
            "max": int(lengths.max()),
            "mean": round(float(lengths.mean()), 1),
        }
    return {
        "n_chunks": len(chunks),
        "n_docs": len(per_doc),
        "chunks_per_doc_median": float(np.median(list(per_doc.values()))) if per_doc else None,
        "n_tokens": tokens,
        "n_tokens_total": int(lengths.sum()),
        "n_at_max_tokens": int((lengths == max_tokens).sum()),
        "n_over_max_tokens": int((lengths > max_tokens).sum()),
        "by_page_type": dict(sorted(Counter(c["page_type"] for c in chunks).items())),
    }


def plot_histogram(chunks: Sequence[Chunk], tag: str, out: Path, max_tokens: int = 512) -> None:
    import matplotlib

    matplotlib.use("Agg")  # no display needed
    import matplotlib.pyplot as plt

    lengths = [c["n_tokens"] for c in chunks]
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.hist(lengths, bins=np.linspace(0, max_tokens, 33), color="#3b6ea5", edgecolor="white")
    ax.axvline(max_tokens, color="#a33", linestyle="--", linewidth=1, label=f"max {max_tokens}")
    ax.set_title(f"Chunk length, {tag} (n = {len(lengths)})")
    ax.set_xlabel("tokens per chunk (bge-small tokenizer)")
    ax.set_ylabel("chunks")
    ax.legend()
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=120)
    plt.close(fig)


def write_stats(tag: str, stats: dict[str, Any], artifacts: Path) -> Path:
    """Add or replace one strategy's entry in chunk_stats.json, keeping the others."""
    out = artifacts / STATS_FILE
    existing = json.loads(out.read_text()) if out.exists() else {}
    existing[tag] = stats | {"written_at": datetime.now(UTC).isoformat(timespec="seconds")}
    artifacts.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(sorted(existing.items())), indent=2) + "\n")
    return out


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chunk counts and token-length stats.")
    parser.add_argument("--strategy", required=True, help="fixed | struct | fixed@v1 | struct@v1")
    parser.add_argument("--chunks", type=Path, help="chunk file (default data/chunks/<tag>.jsonl)")
    parser.add_argument("--artifacts", type=Path, default=paths.ARTIFACTS)
    parser.add_argument("--max-tokens", type=int, default=512)
    args = parser.parse_args(list(argv) if argv is not None else None)

    tag = args.strategy if "@" in args.strategy else f"{args.strategy}@v1"
    chunks = read_chunk_file(args.chunks or paths.chunk_file(tag))
    stats = chunk_stats(chunks, args.max_tokens)
    out = write_stats(tag, stats, args.artifacts)
    png = args.artifacts / f"chunk_hist_{tag}.png"
    plot_histogram(chunks, tag, png, args.max_tokens)

    t = stats["n_tokens"]
    print(
        f"{tag}: {stats['n_chunks']} chunks from {stats['n_docs']} docs; tokens min {t['min']}, "
        f"median {t['median']}, p95 {t['p95']}, max {t['max']}"
    )
    print(f"wrote {out} and {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
