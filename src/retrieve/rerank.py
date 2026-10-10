"""Stage 8: rerank the retrieval pool with a cross-encoder and keep the top 5.

A cross-encoder reads the query and one passage together, so it judges relevance
far better than comparing two separately made embeddings, but it is too slow to
run over the whole corpus. It only reorders the ~20 candidates retrieve() returns,
which is why Recall@20 of that pool is the reranker's ceiling.

rerank_score is the model's raw relevance logit (higher is more relevant). Each
call's latency is kept in `Reranker.last_latency_ms` for the query log and report 04.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from src.common.contracts import Candidate, RerankedCandidate


class Reranker:
    """Wraps a sentence-transformers CrossEncoder. Pass `model` to inject a stand-in in tests."""

    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        batch_size: int = 32,
        model: Any = None,
    ) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self._model = model
        self.last_latency_ms: float = 0.0

    @classmethod
    def from_config(cls, cfg: dict) -> Reranker:
        return cls(cfg["rerank_settings"]["model"])

    @property
    def model(self) -> Any:
        if self._model is None:
            from sentence_transformers import CrossEncoder

            # Runs locally on CPU (CLAUDE.md section 1.1).
            self._model = CrossEncoder(self.model_name, device="cpu")
        return self._model

    def rerank(
        self, query: str, candidates: Sequence[Candidate], top_n: int = 5
    ) -> list[RerankedCandidate]:
        """The top_n candidates by cross-encoder score, each with rerank_score added.

        Ties keep the retrieval order (stable sort), so results are reproducible.
        """
        started = time.perf_counter()
        if not candidates:
            self.last_latency_ms = 0.0
            return []
        scores = self.model.predict(
            [(query, c["text"]) for c in candidates],
            batch_size=self.batch_size,
            show_progress_bar=False,
        )
        scored = [
            RerankedCandidate(**c, rerank_score=float(s))
            for c, s in zip(candidates, scores, strict=True)
        ]
        scored.sort(key=lambda c: -c["rerank_score"])
        self.last_latency_ms = (time.perf_counter() - started) * 1000
        return scored[:top_n]


_default: Reranker | None = None


def rerank(query: str, candidates: Sequence[Candidate], top_n: int = 5) -> list[RerankedCandidate]:
    """The section 5 contract, using the configured cross-encoder."""
    global _default
    if _default is None:
        from src.common.config import load_config

        _default = Reranker.from_config(load_config("default"))
    return _default.rerank(query, candidates, top_n)


def last_latency_ms() -> float:
    """Latency of the most recent rerank() call, for the per-stage timings."""
    return _default.last_latency_ms if _default else 0.0
