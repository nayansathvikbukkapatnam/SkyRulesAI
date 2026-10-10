"""Every file location the pipeline reads or writes, in one place."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DATA = ROOT / "data"
RAW = DATA / "raw"  # gitignored: {sha256}.bin, manifest.jsonl, skipped.jsonl
MANIFEST = RAW / "manifest.jsonl"
SKIPPED = RAW / "skipped.jsonl"
NORMALIZED = DATA / "normalized"  # committed: the source of truth after the crawl
CHUNKS = DATA / "chunks"  # gitignored: {strategy}@{version}.jsonl
INDEX = DATA / "index"  # gitignored: bm25_<strategy>.pkl
TEST_PAGES = DATA / "test_pages"  # labelled synthetic pages (injection test only)
CHROMA = ROOT / "chroma"  # gitignored: Chroma PersistentClient folder

CONFIGS = ROOT / "configs"
EVAL = ROOT / "eval"
GOLDEN = EVAL / "golden.jsonl"
SPLIT = EVAL / "split.json"

REPORTS = ROOT / "reports"
ARTIFACTS = REPORTS / "artifacts"  # every reported number comes from here

LOGS = ROOT / "logs"  # gitignored
QUERY_LOG = LOGS / "queries.jsonl"
CACHE = ROOT / ".cache"  # gitignored
LLM_CACHE = CACHE / "llm"

FIXTURES = ROOT / "tests" / "fixtures"


def raw_path(sha256: str) -> Path:
    return RAW / f"{sha256}.bin"


def normalized_path(doc_id: str) -> Path:
    return NORMALIZED / f"{doc_id}.json"


def chunk_file(strategy_tag: str) -> Path:
    """'struct@v1' -> data/chunks/struct@v1.jsonl"""
    return CHUNKS / f"{strategy_tag}.jsonl"


def bm25_path(strategy_tag: str) -> Path:
    return INDEX / f"bm25_{strategy_tag}.pkl"
