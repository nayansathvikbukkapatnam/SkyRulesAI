"""Stage 3 (baseline): fixed@v1 windows hold at most 512 tokens, overlap by 64, and repeat."""

import copy
import json
import re

import pytest

from src.chunk.fixed import (
    bge_tokenizer,
    chunk_corpus,
    chunk_doc,
    doc_text,
    main,
    read_normalized,
    token_windows,
)
from src.common import paths
from src.common.contracts import parse_chunk_id, validate_chunk

FIX = paths.FIXTURES


def word_tokenize(text):
    """Stands in for the bge tokenizer: one token per whitespace-separated word."""
    return [m.span() for m in re.finditer(r"\S+", text)]


@pytest.fixture
def docs():
    return read_normalized(FIX / "normalized")


def long_doc(docs, n_words):
    """A fixture doc whose one section holds n_words numbered words (w0 w1 ...)."""
    doc = copy.deepcopy(docs[0])
    doc["sections"] = [
        {"heading_path": ["Long"], "text": " ".join(f"w{i}" for i in range(n_words)), "tables": []}
    ]
    return doc


# --- Windows -------------------------------------------------------------------


def test_windows_cover_everything_with_exact_overlap():
    windows = list(token_windows(1200, 512, 64))
    assert windows == [(0, 512), (448, 960), (896, 1200)]


@pytest.mark.parametrize("n", [0, 1, 511, 512])
def test_short_texts_give_at_most_one_window(n):
    assert list(token_windows(n, 512, 64)) == ([(0, n)] if n else [])


def test_no_trailing_window_hidden_inside_the_previous_one():
    # 960 tokens: the second window ends exactly at the end, so no third window.
    assert list(token_windows(960, 512, 64)) == [(0, 512), (448, 960)]


@pytest.mark.parametrize("overlap", [-1, 512, 600])
def test_bad_overlap_is_rejected(overlap):
    with pytest.raises(ValueError):
        list(token_windows(100, 512, overlap))


# --- Chunks --------------------------------------------------------------------


def test_fixture_corpus_gives_valid_chunks(docs):
    chunks = chunk_corpus(docs, word_tokenize)
    assert len(chunks) == len(docs)  # every fixture doc is short: one chunk each
    for chunk in chunks:
        assert validate_chunk(chunk) == []
        assert parse_chunk_id(chunk["chunk_id"])[1:] == ("fixed", "v1", chunk["ordinal"])
        assert chunk["heading_path"] == []


def test_chunk_text_keeps_sections_and_tables(docs):
    register = next(d for d in docs if d["page_type"] == "faq")
    (chunk,) = chunk_doc(register, word_tokenize)
    assert chunk["text"] == doc_text(register)
    assert "| Drone use | Registration | Fee |" in chunk["text"]


def test_long_doc_windows_overlap_by_64_tokens(docs):
    chunks = chunk_doc(long_doc(docs, 1200), word_tokenize)
    words = [c["text"].split() for c in chunks]
    assert [c["n_tokens"] for c in chunks] == [512, 512, 304]
    assert [c["ordinal"] for c in chunks] == [0, 1, 2]
    for prev, nxt in zip(words, words[1:], strict=False):
        assert prev[-64:] == nxt[:64]
    assert words[0][0] == "w0" and words[-1][-1] == "w1199"


def test_chunk_text_is_the_original_substring(docs):
    doc = copy.deepcopy(docs[0])
    doc["sections"][0]["text"] = "Keep   CASE, punctuation;\tand   spacing."
    (chunk,) = chunk_doc(doc, word_tokenize, max_tokens=512)
    assert chunk["text"].startswith("Keep   CASE, punctuation;\tand   spacing.")


def test_empty_doc_gives_no_chunks(docs):
    doc = copy.deepcopy(docs[0])
    doc["sections"] = [{"heading_path": ["Empty"], "text": "  ", "tables": []}]
    assert chunk_doc(doc, word_tokenize) == []


def test_ids_and_hashes_are_reproducible(docs):
    first = chunk_corpus(docs + [long_doc(docs, 1000)], word_tokenize)
    second = chunk_corpus(list(reversed(docs)) + [long_doc(docs, 1000)], word_tokenize)
    key = lambda c: c["chunk_id"]  # noqa: E731
    assert sorted(first, key=key) == sorted(second, key=key)


def test_read_normalized_sorts_by_doc_id_and_rejects_empty_dir(docs, tmp_path):
    assert [d["doc_id"] for d in docs] == sorted(d["doc_id"] for d in docs)
    with pytest.raises(FileNotFoundError):
        read_normalized(tmp_path)


def test_main_writes_chunk_file(tmp_path, capsys):
    out = tmp_path / "fixed@v1.jsonl"
    assert main(["--input", str(FIX / "normalized"), "--output", str(out)], word_tokenize) == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 5 and all(validate_chunk(r) == [] for r in rows)
    assert "fixed@v1: 5 chunks from 5 docs" in capsys.readouterr().out


# --- Real tokenizer (runs where bge-small is already downloaded; skipped in CI) ---


def _bge_cached():
    try:
        from huggingface_hub import snapshot_download

        snapshot_download("BAAI/bge-small-en-v1.5", local_files_only=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _bge_cached(), reason="bge-small-en-v1.5 not in the local HF cache")
def test_real_bge_tokenizer_counts_match(docs):
    tokenize = bge_tokenizer()
    doc = copy.deepcopy(docs[0])
    doc["sections"] = [
        {
            "heading_path": ["Long"],
            "text": " ".join(s["text"] for s in docs[0]["sections"] * 60),
            "tables": [],
        }
    ]
    chunks = chunk_doc(doc, tokenize)
    assert len(chunks) > 1
    assert all(c["n_tokens"] <= 512 for c in chunks)
    assert all(c["n_tokens"] == 512 for c in chunks[:-1])
    for chunk in chunks:
        # Re-tokenizing a chunk's text gives (almost exactly) the count we stored.
        assert abs(len(tokenize(chunk["text"])) - chunk["n_tokens"]) <= 2
