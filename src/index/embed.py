"""Stage 4: turn chunk texts into L2-normalized vectors with bge-small-en-v1.5.

Vectors are normalized to length 1 so cosine similarity (the Chroma collection's
distance) and inner product rank identically. embed() checks every vector is
finite with norm 1 and raises instead of silently storing a bad one.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

NORM_TOLERANCE = 1e-3


def own_weights(model: Any) -> Any:
    """Copy every weight and buffer of a loaded model into memory this process owns.

    With some torch and transformers versions on macOS, weights loaded zero-copy from
    the safetensors file give NaN in matrix multiplies although every value reads as
    finite; one copy after loading fixes it (seen with the cross-encoder: NaN for every
    pair before, -0.34 vs -11.06 after). Copying costs a few MB once, at load.
    """
    import torch

    module = model if isinstance(model, torch.nn.Module) else getattr(model, "model", None)
    if not isinstance(module, torch.nn.Module):
        return model  # a test stand-in: nothing to copy
    with torch.no_grad():
        for param in module.parameters():
            param.data = param.data.clone()
        for sub in module.modules():
            for name, buf in list(sub._buffers.items()):
                if buf is not None:
                    sub._buffers[name] = buf.clone()
    return model


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
            self._model = own_weights(SentenceTransformer(self.model_name, device="cpu"))
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
    nonfinite = np.flatnonzero(~np.isfinite(vectors).all(axis=1))
    if nonfinite.size:  # NaN fails every comparison, so it must be caught explicitly
        raise ValueError(
            f"{nonfinite.size} embedding(s) contain NaN or infinite values "
            f"(first at row {nonfinite[0]}); the model did not load correctly"
        )
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
