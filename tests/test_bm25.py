"""Stage 7 (keyword side): BM25 ranks keyword matches first, filters by page_type, persists."""

import json

import pytest

from src.common import paths
from src.index.store import build_bm25_if_available
from src.retrieve.bm25 import BM25Index, build_bm25, tokenize

FIX = paths.FIXTURES
FIX_CHUNKS = FIX / "chunks" / "struct@v1.jsonl"


@pytest.fixture
def chunks():
    return [json.loads(x) for x in FIX_CHUNKS.read_text().splitlines()]


@pytest.fixture
def index(chunks):
    return BM25Index("struct@v1", chunks)


def test_tokenize_is_lowercase_alphanumeric():
    assert tokenize("PART-107: Fly <= 400 ft, 0.55 lb!") == [
        "part", "107", "fly", "400", "ft", "0", "55", "lb",
    ]  # fmt: skip


def test_keyword_query_ranks_the_matching_chunk_first(index):
    top = index.search("stable air turbulence visibility", k=3)
    assert "Stable and unstable air" in top[0]["text"]
    assert top[0]["bm25_score"] > 0
    assert top[0]["dense_score"] is None and top[0]["fused_score"] is None


def test_results_are_sorted_and_capped(index):
    results = index.search("drone pilots fly", k=4)
    assert 0 < len(results) <= 4
    scores = [r["bm25_score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_chunks_without_query_terms_are_left_out(index):
    assert index.search("zeppelin") == []
    assert index.search("!!!") == []
    results = index.search("registration", k=20)
    assert all("regist" in r["text"].lower() for r in results)


def test_page_type_filter(index):
    results = index.search("drone fixture text", k=20, page_type="faq")
    assert results and {r["page_type"] for r in results} == {"faq"}
    both = index.search("drone fixture text", k=20, page_type=["faq", "study_guide"])
    assert {r["page_type"] for r in both} <= {"faq", "study_guide"}


def test_candidate_has_contract_fields(index):
    (top,) = index.search("Remote ID broadcast module", k=1)
    assert set(top) == {
        "chunk_id", "text", "source_url", "title", "effective_date", "page_type",
        "dense_score", "bm25_score", "fused_score",
    }  # fmt: skip


def test_build_save_and_load_round_trip(tmp_path):
    out = tmp_path / "bm25_struct@v1.pkl"
    built = build_bm25("struct@v1", FIX_CHUNKS, out)
    loaded = BM25Index.load("struct@v1", out)
    assert len(loaded) == len(built) == 9
    assert loaded.search("safety test") == built.search("safety test")


def test_load_rejects_wrong_tag_and_missing_file(tmp_path):
    out = tmp_path / "bm25.pkl"
    build_bm25("struct@v1", FIX_CHUNKS, out)
    with pytest.raises(ValueError):
        BM25Index.load("fixed@v1", out)
    with pytest.raises(FileNotFoundError, match="make index"):
        BM25Index.load("struct@v1", tmp_path / "missing.pkl")


def test_store_hook_builds_bm25(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CHUNKS", FIX / "chunks")
    monkeypatch.setattr(paths, "INDEX", tmp_path)
    assert build_bm25_if_available("struct@v1") == "built"
    assert (tmp_path / "bm25_struct@v1.pkl").exists()


def test_empty_chunk_list_is_rejected():
    with pytest.raises(ValueError):
        BM25Index("struct@v1", [])
