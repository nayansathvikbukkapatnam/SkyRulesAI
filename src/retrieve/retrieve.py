"""Stage 7: retrieve(): dense (Chroma), BM25, or both fused with Reciprocal Rank Fusion.

RRF scores a chunk by its rank in each list, not its raw score, so cosine
similarities (0..1) and BM25 scores (unbounded) never need to be put on one scale:

    fused_score(chunk) = sum over lists containing it of 1 / (rrf_k + rank),   rank from 1

With rrf_k = 60, a chunk ranked 1st by both scores 2/61 = 0.0328. Each side
contributes a pool of `retrieval.pool_size` (20) candidates, and the fused list
is cut to k. Every Candidate carries dense_score, bm25_score and fused_score;
a score is None when that retriever did not return the chunk (or was not run).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from src.common.config import load_config
from src.common.contracts import RETRIEVAL_MODES, Candidate
from src.retrieve.bm25 import BM25Index

FILTER_KEYS = frozenset({"page_type"})


def rrf_fuse(
    ranked_lists: dict[str, Sequence[Candidate]], k: int = 20, rrf_k: int = 60
) -> list[Candidate]:
    """Fuse ranked lists keyed by score name ("dense", "bm25") into one ranking.

    A chunk's dense_score / bm25_score is copied from the list that returned it.
    Ties on fused_score are broken by best single rank, then chunk_id, so the
    order is reproducible.
    """
    merged: dict[str, dict[str, Any]] = {}
    best_rank: dict[str, int] = {}
    for name, candidates in ranked_lists.items():
        for rank, cand in enumerate(candidates, start=1):
            cid = cand["chunk_id"]
            entry = merged.setdefault(
                cid, {**cand, "dense_score": None, "bm25_score": None, "fused_score": 0.0}
            )
            entry[f"{name}_score"] = cand[f"{name}_score"]
            entry["fused_score"] += 1.0 / (rrf_k + rank)
            best_rank[cid] = min(best_rank.get(cid, rank), rank)
    order = sorted(merged, key=lambda cid: (-merged[cid]["fused_score"], best_rank[cid], cid))
    return [Candidate(**merged[cid]) for cid in order[:k]]


class Retriever:
    """Holds the Chroma store and BM25 indexes (loaded once per strategy).

    `store` is anything with ChunkStore.query's signature; tests pass a stand-in.
    """

    def __init__(self, cfg: dict, store: Any = None, bm25: dict[str, BM25Index] | None = None):
        self.cfg = cfg
        self._store = store
        self._bm25 = dict(bm25 or {})

    @property
    def store(self) -> Any:
        if self._store is None:
            from src.index.store import ChunkStore

            self._store = ChunkStore.open(self.cfg)
        return self._store

    def bm25(self, strategy: str) -> BM25Index:
        if strategy not in self._bm25:
            self._bm25[strategy] = BM25Index.load(strategy)
        return self._bm25[strategy]

    def retrieve(
        self,
        query: str,
        k: int = 20,
        mode: str = "hybrid",
        filters: dict | None = None,
        strategy: str = "struct@v1",
    ) -> list[Candidate]:
        if mode not in RETRIEVAL_MODES:
            raise ValueError(f"mode must be one of {sorted(RETRIEVAL_MODES)}: {mode!r}")
        unknown = set(filters or {}) - FILTER_KEYS
        if unknown:
            raise ValueError(
                f"unsupported filter(s) {sorted(unknown)}; allowed: {sorted(FILTER_KEYS)}"
            )
        page_type = (filters or {}).get("page_type")
        pool = max(k, self.cfg["retrieval"]["pool_size"])

        if mode == "dense":
            return self.store.query(query, k=k, strategy=strategy, page_type=page_type)
        if mode == "bm25":
            return self.bm25(strategy).search(query, k=k, page_type=page_type)
        dense = self.store.query(query, k=pool, strategy=strategy, page_type=page_type)
        keyword = self.bm25(strategy).search(query, k=pool, page_type=page_type)
        return rrf_fuse(
            {"dense": dense, "bm25": keyword}, k=k, rrf_k=self.cfg["retrieval"]["rrf_k"]
        )


_default: Retriever | None = None


def retrieve(
    query: str,
    k: int = 20,
    mode: str = "hybrid",
    filters: dict | None = None,
    strategy: str = "struct@v1",
) -> list[Candidate]:
    """The section 5 contract. Uses configs/default.yaml's retrieval settings."""
    global _default
    if _default is None:
        _default = Retriever(load_config("default"))
    return _default.retrieve(query, k=k, mode=mode, filters=filters, strategy=strategy)
