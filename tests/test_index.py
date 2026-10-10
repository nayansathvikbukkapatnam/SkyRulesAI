"""Stage 4 and 5: embeddings are normalized, the collection is cosine, upserts are idempotent."""

import hashlib
import json

import numpy as np
import pytest

from src.common import paths
from src.index.embed import Embedder, check_normalized
from src.index.store import (
    ChunkStore,
    assert_cosine,
    build_where,
    collection_space,
    get_client,
    open_collection,
)

FIX = paths.FIXTURES


def load_chunks(tag):
    return [json.loads(x) for x in (FIX / "chunks" / f"{tag}.jsonl").read_text().splitlines()]


class FakeModel:
    """Stands in for SentenceTransformer: bag-of-words vectors, normalized only if asked."""

    dim = 32

    def __init__(self, ignore_normalize=False):
        self.ignore_normalize = ignore_normalize
        self.calls = []

    def get_embedding_dimension(self):
        return self.dim

    def encode(self, texts, normalize_embeddings=False, **kwargs):
        self.calls.append({"normalize_embeddings": normalize_embeddings, **kwargs})
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in text.lower().split():
                out[i, int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1.0
            out[i] += 0.01  # never an all-zero vector
        if normalize_embeddings and not self.ignore_normalize:
            out /= np.linalg.norm(out, axis=1, keepdims=True)
        return out


@pytest.fixture
def store(tmp_path):
    collection = open_collection(get_client(tmp_path / "chroma"), "skyrules")
    return ChunkStore(collection, Embedder("fake", model=FakeModel()))


# --- Embedding ---------------------------------------------------------------


def test_embed_requests_normalization_and_returns_unit_vectors():
    model = FakeModel()
    vectors = Embedder("fake", model=model).embed(["fly at or below 400 feet", "register"])
    assert vectors.shape == (2, FakeModel.dim)
    assert vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-3)
    assert all(call["normalize_embeddings"] is True for call in model.calls)


def test_embed_refuses_unnormalized_vectors():
    with pytest.raises(ValueError, match="not L2-normalized"):
        Embedder("fake", model=FakeModel(ignore_normalize=True)).embed(["some text"])


def test_check_normalized_tolerance():
    check_normalized(np.array([[1.0005, 0.0]]))  # within 1e-3
    with pytest.raises(ValueError):
        check_normalized(np.array([[1.01, 0.0]]))


def test_query_instruction_is_prefixed_to_queries_only():
    model = FakeModel()
    emb = Embedder("fake", query_instruction="Q: ", model=model)
    calls = []
    model.encode = lambda texts, **kw: calls.append(texts) or FakeModel.encode(model, texts, **kw)
    emb.embed(["passage"])
    emb.embed_query("question")
    assert calls == [["passage"], ["Q: question"]]


def test_empty_input_gives_empty_matrix():
    assert Embedder("fake", model=FakeModel()).embed([]).shape == (0, FakeModel.dim)


# --- Collection metric -------------------------------------------------------


def test_collection_uses_cosine_not_chromas_l2_default(tmp_path):
    client = get_client(tmp_path / "chroma")
    collection = open_collection(client, "skyrules")
    assert collection_space(collection) == "cosine"
    # Reopening keeps the setting (it is stored with the collection).
    assert collection_space(get_client(tmp_path / "chroma").get_collection("skyrules")) == "cosine"


def test_assert_cosine_rejects_an_l2_collection(tmp_path):
    l2 = get_client(tmp_path / "chroma").get_or_create_collection("plain", embedding_function=None)
    assert collection_space(l2) == "l2"
    with pytest.raises(RuntimeError, match="expected 'cosine'"):
        assert_cosine(l2)


# --- Upsert, filter, delete ----------------------------------------------------


def test_reupserting_the_same_chunks_keeps_the_count(store):
    chunks = load_chunks("struct@v1")
    store.upsert(chunks)
    store.upsert(chunks)
    assert store.count() == len(chunks)
    assert store.ids() == {c["chunk_id"] for c in chunks}


def test_stored_vectors_have_norm_one(store):
    store.upsert(load_chunks("struct@v1"))
    stored = np.asarray(store.collection.get(include=["embeddings"])["embeddings"])
    assert np.allclose(np.linalg.norm(stored, axis=1), 1.0, atol=1e-3)


def test_page_type_filter_returns_only_that_type(store):
    store.upsert(load_chunks("struct@v1"))
    hits = store.query("weather and visibility", k=20, page_type="study_guide")
    assert hits and {h["page_type"] for h in hits} == {"study_guide"}
    several = store.query("drone", k=20, page_type=["faq", "regulation_summary"])
    assert {h["page_type"] for h in several} <= {"faq", "regulation_summary"}


def test_strategy_filter_and_delete_by_strategy(store):
    fixed, struct = load_chunks("fixed@v1"), load_chunks("struct@v1")
    store.upsert(fixed)
    store.upsert(struct)
    assert store.count({"strategy": "fixed@v1"}) == len(fixed)
    assert {h["chunk_id"] for h in store.query("drone", k=50, strategy="struct@v1")} <= {
        c["chunk_id"] for c in struct
    }
    assert store.delete({"strategy": "fixed@v1"}) == len(fixed)
    assert store.count({"strategy": "fixed@v1"}) == 0
    assert store.count() == len(struct)


def test_delete_without_a_filter_is_refused(store):
    with pytest.raises(ValueError):
        store.delete({})


def test_sync_removes_stale_chunks_of_the_same_strategy(store):
    chunks = load_chunks("struct@v1")
    store.upsert(chunks)
    stats = store.sync(chunks[:-2], "struct@v1")
    assert stats == {"upserted": len(chunks) - 2, "stale_removed": 2, "total": len(chunks) - 2}


def test_query_returns_candidates_in_the_contract_shape(store):
    store.upsert(load_chunks("struct@v1"))
    hits = store.query("how high can recreational flyers fly 400 feet", k=5)
    assert len(hits) == 5
    scores = [h["dense_score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(-1.0 <= s <= 1.0 + 1e-6 for s in scores)
    assert set(hits[0]) == {
        "chunk_id",
        "text",
        "source_url",
        "title",
        "effective_date",
        "page_type",
        "dense_score",
        "bm25_score",
        "fused_score",
    }
    no_date = [h for h in store.query("register", k=20) if h["page_type"] == "faq"]
    assert no_date and no_date[0]["effective_date"] is None


def test_build_where():
    assert build_where() is None
    assert build_where(strategy="fixed@v1") == {"strategy": "fixed@v1"}
    assert build_where("fixed@v1", "faq") == {
        "$and": [{"strategy": "fixed@v1"}, {"page_type": "faq"}]
    }
    assert build_where(page_type=["faq"]) == {"page_type": {"$in": ["faq"]}}


# --- Real model (runs where bge-small is already downloaded; skipped in CI) ---


def _bge_cached():
    try:
        from huggingface_hub import snapshot_download

        snapshot_download("BAAI/bge-small-en-v1.5", local_files_only=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _bge_cached(), reason="bge-small-en-v1.5 not in the local HF cache")
def test_real_bge_model_is_384_dim_normalized_and_sensible(tmp_path):
    collection = open_collection(get_client(tmp_path / "chroma"), "skyrules")
    store = ChunkStore(
        collection,
        Embedder(query_instruction="Represent this sentence for searching relevant passages: "),
    )
    chunks = load_chunks("struct@v1")
    vectors = store.embedder.embed([c["text"] for c in chunks])
    assert vectors.shape == (len(chunks), 384)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-3)
    store.upsert(chunks)
    top = store.query("How high can I fly my drone for fun?", k=1)[0]
    assert "400 feet" in top["text"]
