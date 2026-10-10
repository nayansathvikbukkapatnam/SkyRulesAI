"""Stage 7: retrieve() returns dense, BM25 or RRF-fused candidates with all three scores."""

import json

import pytest

from src.common import paths
from src.common.config import load_config
from src.index.embed import Embedder
from src.index.store import ChunkStore, get_client, open_collection
from src.retrieve.bm25 import BM25Index
from src.retrieve.retrieve import Retriever, rrf_fuse
from tests.test_index import FakeModel

FIX_CHUNKS = paths.FIXTURES / "chunks" / "struct@v1.jsonl"


def cand(cid, **scores):
    base = {"text": "", "source_url": "u", "title": "t", "effective_date": None, "page_type": "faq"}
    return {
        "chunk_id": cid,
        **base,
        "dense_score": scores.get("dense"),
        "bm25_score": scores.get("bm25"),
        "fused_score": None,
    }


# --- RRF -----------------------------------------------------------------------


def test_rrf_scores_and_order():
    dense = [cand("a", dense=0.9), cand("b", dense=0.8), cand("c", dense=0.7)]
    bm25 = [cand("b", bm25=9.0), cand("d", bm25=5.0)]
    fused = rrf_fuse({"dense": dense, "bm25": bm25}, k=10, rrf_k=60)
    assert [c["chunk_id"] for c in fused] == ["b", "a", "d", "c"]
    b = fused[0]
    assert b["fused_score"] == pytest.approx(1 / 62 + 1 / 61)
    assert (b["dense_score"], b["bm25_score"]) == (0.8, 9.0)
    a, d = fused[1], fused[2]
    assert a["fused_score"] == pytest.approx(1 / 61) and a["bm25_score"] is None
    assert d["fused_score"] == pytest.approx(1 / 62) and d["dense_score"] is None


def test_rrf_ties_are_broken_reproducibly():
    fused = rrf_fuse({"dense": [cand("z", dense=1.0)], "bm25": [cand("y", bm25=1.0)]}, k=5)
    assert [c["chunk_id"] for c in fused] == ["y", "z"]  # same score and rank: by chunk_id


def test_rrf_cuts_to_k_and_handles_empty_lists():
    dense = [cand(f"c{i}", dense=1 - i / 100) for i in range(30)]
    assert len(rrf_fuse({"dense": dense, "bm25": []}, k=20)) == 20
    assert rrf_fuse({"dense": [], "bm25": []}) == []


# --- Retriever on the fixtures (fake embedder, real Chroma and BM25) -----------


@pytest.fixture
def retriever(tmp_path):
    chunks = [json.loads(x) for x in FIX_CHUNKS.read_text().splitlines()]
    store = ChunkStore(
        open_collection(get_client(tmp_path / "chroma"), "skyrules"),
        Embedder("fake", model=FakeModel()),
    )
    store.upsert(chunks)
    return Retriever(load_config("default"), store, {"struct@v1": BM25Index("struct@v1", chunks)})


def test_hybrid_returns_candidates_with_all_scores(retriever):
    results = retriever.retrieve("stable air turbulence", k=5)
    assert 0 < len(results) <= 5
    assert "Stable and unstable air" in results[0]["text"]
    assert all(r["fused_score"] is not None for r in results)
    assert results[0]["dense_score"] is not None and results[0]["bm25_score"] is not None
    fused = [r["fused_score"] for r in results]
    assert fused == sorted(fused, reverse=True)


def test_dense_and_bm25_modes(retriever):
    dense = retriever.retrieve("safety test", k=3, mode="dense")
    assert len(dense) == 3 and all(r["bm25_score"] is None for r in dense)
    bm25 = retriever.retrieve("safety test", k=3, mode="bm25")
    assert bm25 and all(r["dense_score"] is None for r in bm25)


def test_page_type_filter_applies_to_both_sides(retriever):
    results = retriever.retrieve("drone fixture", k=20, filters={"page_type": "faq"})
    assert results and {r["page_type"] for r in results} == {"faq"}


def test_hybrid_pool_holds_every_chunk_from_either_side(retriever):
    results = retriever.retrieve("remote pilot certificate", k=20)
    ids = [r["chunk_id"] for r in results]
    assert len(ids) == len(set(ids))
    dense_ids = {r["chunk_id"] for r in retriever.retrieve("remote pilot certificate", 20, "dense")}
    bm25_ids = {r["chunk_id"] for r in retriever.retrieve("remote pilot certificate", 20, "bm25")}
    assert set(ids) == dense_ids | bm25_ids


def test_bad_mode_and_filter_are_rejected(retriever):
    with pytest.raises(ValueError):
        retriever.retrieve("x", mode="magic")
    with pytest.raises(ValueError):
        retriever.retrieve("x", filters={"audience": "part107"})


def test_opening_the_store_loads_the_embedding_model(monkeypatch):
    # The app warms up by touching `store`; the slow part is the embedding model.
    loaded = []

    class LazyEmbedder:
        @property
        def model(self):
            loaded.append("bge")
            return object()

    class Store:
        embedder = LazyEmbedder()

    monkeypatch.setattr("src.index.store.ChunkStore.open", classmethod(lambda cls, cfg: Store()))
    retriever = Retriever(load_config("default"))
    _ = retriever.store
    assert loaded == ["bge"]
    _ = retriever.store  # opened once
    assert loaded == ["bge"]


def test_warm_up_loads_bm25_for_each_strategy(retriever, monkeypatch):
    seen = []
    monkeypatch.setattr(BM25Index, "load", classmethod(lambda cls, tag: seen.append(tag) or tag))
    retriever.warm_up(("struct@v1", "fixed@v1"))
    assert seen == ["fixed@v1"]  # struct@v1 was already loaded by the fixture
