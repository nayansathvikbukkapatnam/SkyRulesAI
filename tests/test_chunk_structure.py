"""Stage 3 (structure-aware): chunks follow sections, carry headings, and keep tables readable."""

import copy
import json
import re

import pytest

from src.chunk.fixed import bge_tokenizer, read_normalized
from src.chunk.structure import (
    chunk_corpus,
    chunk_doc,
    heading_prefix,
    main,
    split_table,
)
from src.common import paths
from src.common.contracts import parse_chunk_id, validate_chunk

FIX = paths.FIXTURES


def word_tokenize(text):
    """Stands in for the bge tokenizer: one token per whitespace-separated word."""
    return [m.span() for m in re.finditer(r"\S+", text)]


def n_words(text):
    return len(word_tokenize(text))


@pytest.fixture
def docs():
    return read_normalized(FIX / "normalized")


def with_sections(doc, sections):
    doc = copy.deepcopy(doc)
    doc["sections"] = sections
    return doc


def table(n_rows, cells=3):
    header = "| " + " | ".join(f"h{i}" for i in range(cells)) + " |"
    sep = "| " + " | ".join("---" for _ in range(cells)) + " |"
    rows = ["| " + " | ".join(f"r{r}c{c}" for c in range(cells)) + " |" for r in range(n_rows)]
    return "\n".join([header, sep, *rows])


# --- Fixture corpus ------------------------------------------------------------


def test_one_chunk_per_fixture_section_with_heading_path(docs):
    chunks = chunk_corpus(docs, word_tokenize)
    assert len(chunks) == sum(len(d["sections"]) for d in docs)
    for chunk in chunks:
        assert validate_chunk(chunk) == []
        assert parse_chunk_id(chunk["chunk_id"])[1:3] == ("struct", "v1")
        assert chunk["text"].startswith(" > ".join(chunk["heading_path"]) + "\n\n")
        assert chunk["n_tokens"] == n_words(chunk["text"])


def test_fixture_chunks_match_the_hand_built_struct_fixture(docs):
    expected = [
        json.loads(x) for x in (FIX / "chunks" / "struct@v1.jsonl").read_text().splitlines()
    ]
    chunks = chunk_corpus(docs, word_tokenize)
    key = lambda c: c["chunk_id"]  # noqa: E731
    got = {c["chunk_id"]: (c["text"], c["heading_path"]) for c in sorted(chunks, key=key)}
    want = {c["chunk_id"]: (c["text"], c["heading_path"]) for c in sorted(expected, key=key)}
    assert got == want


def test_small_table_stays_whole_with_its_section(docs):
    register = next(d for d in docs if d["page_type"] == "faq")
    (chunk,) = chunk_doc(register, word_tokenize)
    assert register["sections"][0]["tables"][0] in chunk["text"]


# --- Oversized sections --------------------------------------------------------


def test_long_section_is_windowed_and_every_part_keeps_the_heading(docs):
    text = " ".join(f"w{i}" for i in range(1500))
    doc = with_sections(
        docs[0], [{"heading_path": ["Part 107", "Waivers"], "text": text, "tables": []}]
    )
    chunks = chunk_doc(doc, word_tokenize)
    assert len(chunks) > 1
    assert all(c["text"].startswith("Part 107 > Waivers\n\n") for c in chunks)
    assert all(c["n_tokens"] <= 512 for c in chunks)
    bodies = [c["text"].split("\n\n", 1)[1].split() for c in chunks]
    for prev, nxt in zip(bodies, bodies[1:], strict=False):
        assert prev[-64:] == nxt[:64]
    assert bodies[0][0] == "w0" and bodies[-1][-1] == "w1499"


def test_big_table_is_split_by_rows_repeating_the_header(docs):
    big = table(300)
    doc = with_sections(docs[0], [{"heading_path": ["Fees"], "text": "", "tables": [big]}])
    chunks = chunk_doc(doc, word_tokenize)
    assert len(chunks) > 1
    header = "\n".join(big.splitlines()[:2])
    rows_seen = []
    for chunk in chunks:
        assert chunk["n_tokens"] <= 512
        body = chunk["text"].removeprefix("Fees\n\n")
        assert body.startswith(header + "\n")
        rows_seen += body.splitlines()[2:]
    assert rows_seen == big.splitlines()[2:]  # every row once, in order, none cut


def test_text_and_table_split_separately_when_section_is_too_big(docs):
    text = " ".join(f"w{i}" for i in range(600))
    small = table(3)
    doc = with_sections(docs[0], [{"heading_path": ["S"], "text": text, "tables": [small]}])
    chunks = chunk_doc(doc, word_tokenize)
    assert chunks[-1]["text"] == "S\n\n" + small
    assert all("| h0 |" not in c["text"] for c in chunks[:-1])


def test_split_table_handles_one_giant_row():
    giant = "| h |\n| --- |\n| " + " ".join(f"x{i}" for i in range(700)) + " |\n| small |"
    parts = split_table(giant, word_tokenize, budget=512, overlap=64)
    assert all(n_words(p) <= 512 for p in parts)
    assert parts[-1] == "| h |\n| --- |\n| small |"


def test_non_table_text_in_tables_falls_back_to_windows():
    parts = split_table(" ".join(["word"] * 1000), word_tokenize, budget=512, overlap=64)
    assert [n_words(p) for p in parts] == [512, 512, 104]


# --- Headings and edge cases ---------------------------------------------------


def test_long_heading_path_keeps_only_the_last_heading():
    path = ["very " * 200 + "long", "Short title"]
    assert heading_prefix(path, word_tokenize, 512) == "Short title\n\n"
    assert heading_prefix([], word_tokenize, 512) == ""


def test_section_without_heading_has_no_prefix(docs):
    doc = with_sections(docs[0], [{"heading_path": [], "text": "Just text.", "tables": []}])
    (chunk,) = chunk_doc(doc, word_tokenize)
    assert chunk["text"] == "Just text." and chunk["heading_path"] == []


def test_empty_sections_are_skipped_and_ordinals_stay_dense(docs):
    doc = with_sections(
        docs[0],
        [
            {"heading_path": ["A"], "text": "one", "tables": []},
            {"heading_path": ["B"], "text": "   ", "tables": []},
            {"heading_path": ["C"], "text": "two", "tables": []},
        ],
    )
    chunks = chunk_doc(doc, word_tokenize)
    assert [c["ordinal"] for c in chunks] == [0, 1]
    assert [c["heading_path"] for c in chunks] == [["A"], ["C"]]


def test_output_is_reproducible(docs):
    first = chunk_corpus(docs, word_tokenize)
    second = chunk_corpus(docs, word_tokenize)
    assert first == second


def test_main_writes_chunk_file(tmp_path, capsys):
    out = tmp_path / "struct@v1.jsonl"
    assert main(["--input", str(FIX / "normalized"), "--output", str(out)], word_tokenize) == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 9 and all(validate_chunk(r) == [] for r in rows)
    assert "struct@v1: 9 chunks from 5 docs" in capsys.readouterr().out


# --- Real tokenizer (runs where bge-small is already downloaded; skipped in CI) ---


def _bge_cached():
    try:
        from huggingface_hub import snapshot_download

        snapshot_download("BAAI/bge-small-en-v1.5", local_files_only=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _bge_cached(), reason="bge-small-en-v1.5 not in the local HF cache")
def test_real_bge_tokenizer_keeps_chunks_within_512(docs):
    tokenize = bge_tokenizer()
    long_text = " ".join(s["text"] for s in docs[0]["sections"] * 60)
    doc = with_sections(
        docs[0], [{"heading_path": ["Long", "Section"], "text": long_text, "tables": [table(400)]}]
    )
    chunks = chunk_doc(doc, tokenize)
    assert len(chunks) > 2
    assert all(c["n_tokens"] <= 512 for c in chunks)
    assert all(c["text"].startswith("Long > Section\n\n") for c in chunks)
