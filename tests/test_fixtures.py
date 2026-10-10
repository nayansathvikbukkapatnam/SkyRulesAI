"""The fixtures every stage tests against must themselves obey the contracts."""

import json

from src.common import paths
from src.common.contracts import parse_chunk_id, validate_chunk, validate_normalized_doc

FIX = paths.FIXTURES


def load_docs():
    return [json.loads(p.read_text()) for p in sorted((FIX / "normalized").glob("*.json"))]


def load_chunks(tag):
    return [json.loads(line) for line in (FIX / "chunks" / f"{tag}.jsonl").read_text().splitlines()]


def test_five_fixture_docs_match_the_contract():
    docs = load_docs()
    assert len(docs) == 5
    for doc in docs:
        assert validate_normalized_doc(doc) == []
        assert doc["title"].startswith("[FIXTURE]")
        assert ".test/" in doc["source_url"]  # never a real faa.gov URL
        assert (FIX / "normalized" / f"{doc['doc_id']}.json").exists()


def test_fixtures_cover_the_cases_stages_need():
    docs = load_docs()
    assert any(s["tables"] for d in docs for s in d["sections"]), "need a table"
    assert any(d["page_type"] == "study_guide" for d in docs), "need a PDF-style doc"
    assert any(d["effective_date"] is None for d in docs), "need a doc with no date"
    assert {d["audience"] for d in docs} >= {"recreational", "part107"}


def test_fixture_chunks_match_the_contract():
    doc_ids = {d["doc_id"] for d in load_docs()}
    for tag in ("fixed@v1", "struct@v1"):
        chunks = load_chunks(tag)
        assert chunks
        ids = [c["chunk_id"] for c in chunks]
        assert len(ids) == len(set(ids)), "chunk IDs must be unique"
        for chunk in chunks:
            assert validate_chunk(chunk) == []
            assert chunk["doc_id"] in doc_ids
            assert f"{chunk['strategy']}@{chunk['version']}" == tag
            parse_chunk_id(chunk["chunk_id"])


def test_fixture_candidates_reference_real_fixture_chunks():
    data = json.loads((FIX / "candidates.json").read_text())
    struct_ids = {c["chunk_id"] for c in load_chunks("struct@v1")}
    assert data["question"]
    for cand in data["candidates"]:
        assert cand["chunk_id"] in struct_ids
        assert {"dense_score", "bm25_score", "fused_score"} <= set(cand)


def test_fixtures_are_up_to_date_with_the_builder():
    from tests.fixtures.build_fixtures import DOCS, build_doc

    on_disk = {d["doc_id"]: d for d in load_docs()}
    for spec in DOCS:
        built = build_doc(spec)
        assert on_disk[built["doc_id"]] == built, "re-run: python -m tests.fixtures.build_fixtures"
