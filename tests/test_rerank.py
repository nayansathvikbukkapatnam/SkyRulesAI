"""Stage 8: the cross-encoder reorders the pool, keeps the top 5 and records its latency."""

import copy
import json

import numpy as np
import pytest

from src.common import paths
from src.retrieve.rerank import Reranker

CANDIDATES = json.loads((paths.FIXTURES / "candidates.json").read_text())


class FakeCrossEncoder:
    """Stands in for CrossEncoder: scores a pair by how many query words the passage has."""

    def __init__(self):
        self.calls = []

    def predict(self, pairs, **kwargs):
        self.calls.append(kwargs)
        return np.array(
            [sum(w in text.lower() for w in query.lower().split()) for query, text in pairs],
            dtype=np.float32,
        )


@pytest.fixture
def candidates():
    return copy.deepcopy(CANDIDATES["candidates"])


def test_reorders_by_cross_encoder_score_and_keeps_top_n(candidates):
    reranker = Reranker(model=FakeCrossEncoder())
    top = reranker.rerank("knowledge test remote pilot certificate", candidates, top_n=3)
    assert len(top) == 3
    assert top[0]["chunk_id"] == "a71ad3767684::struct@v1::0001"  # 5th in retrieval order
    scores = [c["rerank_score"] for c in top]
    assert scores == sorted(scores, reverse=True)


def test_keeps_retrieval_fields_and_adds_rerank_score(candidates):
    (top,) = Reranker(model=FakeCrossEncoder()).rerank("400 feet", candidates, top_n=1)
    original = next(c for c in candidates if c["chunk_id"] == top["chunk_id"])
    assert {k: top[k] for k in original} == original
    assert isinstance(top["rerank_score"], float)


def test_ties_keep_retrieval_order(candidates):
    top = Reranker(model=FakeCrossEncoder()).rerank("zeppelin", candidates, top_n=5)
    assert [c["chunk_id"] for c in top] == [c["chunk_id"] for c in candidates[:5]]


def test_fewer_candidates_than_top_n_and_empty_pool(candidates):
    reranker = Reranker(model=FakeCrossEncoder())
    assert len(reranker.rerank("drone", candidates[:2], top_n=5)) == 2
    assert reranker.rerank("drone", [], top_n=5) == []
    assert reranker.last_latency_ms == 0.0


def test_records_latency_and_disables_progress_bar(candidates):
    model = FakeCrossEncoder()
    reranker = Reranker(model=model)
    reranker.rerank("drone", candidates)
    assert reranker.last_latency_ms > 0
    assert model.calls[0]["show_progress_bar"] is False


# --- Real model (runs where the cross-encoder is already downloaded; skipped in CI) ---


def _ce_cached():
    try:
        from huggingface_hub import snapshot_download

        snapshot_download("cross-encoder/ms-marco-MiniLM-L-6-v2", local_files_only=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ce_cached(), reason="ms-marco-MiniLM-L-6-v2 not in the local HF cache")
def test_real_cross_encoder_ranks_the_answer_near_the_top(candidates):
    top = Reranker().rerank(CANDIDATES["question"], candidates, top_n=5)
    assert len(top) == 5
    # On these tiny fixtures MiniLM puts the registration chunk (its table mentions
    # recreational drones) just above the 400-feet answer, so only check the top 2.
    assert any("400 feet" in c["text"] for c in top[:2])
    assert top[0]["rerank_score"] > top[-1]["rerank_score"]


class NaNCrossEncoder:
    """A model that returns NaN, as the real cross-encoder did with zero-copy weights."""

    def predict(self, pairs, **kwargs):
        return np.full(len(pairs), np.nan, dtype=np.float32)


def test_non_finite_scores_raise_instead_of_ranking_silently(candidates):
    with pytest.raises(RuntimeError, match="non-finite scores"):
        Reranker(model=NaNCrossEncoder()).rerank("400 feet", candidates, top_n=5)
