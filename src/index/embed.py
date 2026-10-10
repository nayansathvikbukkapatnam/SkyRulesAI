"""Stage 4: turn chunk texts into L2-normalized vectors with bge-small-en-v1.5.

Vectors are normalized to length 1 so cosine similarity (the Chroma collection's
distance) and inner product rank identically. embed() checks every norm and
raises instead of silently storing an unnormalized vector.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

NORM_TOLERANCE = 1e-3


class Embedder:
    """Wraps a SentenceTransformer. Pass `model` to inject a stand-in in tests."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
        batch_size: int = 64,
        query_instruction: str = "",
        model: Any = None,
    ) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self.query_instruction = query_instruction
        self._model = model

    @classmethod
    def from_config(cls, cfg: dict) -> Embedder:
        e = cfg["embedding"]
        return cls(e["model"], e["batch_size"], e.get("query_instruction", ""))

    @property
    def model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            # Embeddings run locally on CPU (CLAUDE.md section 1.1).
            self._model = SentenceTransformer(self.model_name, device="cpu")
        return self._model

    @property
    def dimension(self) -> int:
        return int(self.model.get_embedding_dimension())

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Embed passages. Returns float32 (n, dimension), each row of norm 1."""
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        vectors = self.model.encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=len(texts) > 4 * self.batch_size,
        )
        vectors = np.asarray(vectors, dtype=np.float32)
        check_normalized(vectors)
        return vectors

    def embed_query(self, text: str) -> np.ndarray:
        """Embed one search query, with bge's retrieval instruction prefixed."""
        return self.embed([self.query_instruction + text])[0]


def check_normalized(vectors: np.ndarray, tol: float = NORM_TOLERANCE) -> None:
    norms = np.linalg.norm(vectors, axis=1)
    bad = np.flatnonzero(np.abs(norms - 1.0) > tol)
    if bad.size:
        raise ValueError(
            f"{bad.size} embedding(s) are not L2-normalized (first norm {norms[bad[0]]:.4f}); "
            "cosine and inner-product rankings would no longer match"
        )


def model_revision(model_name: str) -> str | None:
    """The Hugging Face commit hash of the cached model, recorded for reproducibility."""
    try:
        from huggingface_hub import snapshot_download

        return Path(snapshot_download(model_name, local_files_only=True)).name
    except Exception:
        return None
