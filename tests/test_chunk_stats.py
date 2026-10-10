"""Chunk stats: counts and token-length percentiles are right, and strategies sit side by side."""

import json

import pytest

from src.chunk.stats import chunk_stats, main, read_chunk_file, write_stats
from src.common import paths

FIX = paths.FIXTURES


def fake_chunks(lengths, doc_ids=None, page_type="faq"):
    doc_ids = doc_ids or ["a" * 12] * len(lengths)
    return [
        {"doc_id": d, "n_tokens": n, "page_type": page_type}
        for n, d in zip(lengths, doc_ids, strict=True)
    ]


def test_distribution_numbers():
    stats = chunk_stats(fake_chunks(list(range(1, 101))))
    assert stats["n_chunks"] == 100
    assert stats["n_tokens"] == {"min": 1, "median": 50.5, "p95": 95.05, "max": 100, "mean": 50.5}
    assert stats["n_tokens_total"] == 5050


def test_counts_docs_page_types_and_full_windows():
    chunks = fake_chunks([512, 512, 100], ["a" * 12, "a" * 12, "b" * 12])
    chunks[2]["page_type"] = "guide_page"
    stats = chunk_stats(chunks)
    assert stats["n_docs"] == 2
    assert stats["chunks_per_doc_median"] == 1.5
    assert stats["n_at_max_tokens"] == 2 and stats["n_over_max_tokens"] == 0
    assert stats["by_page_type"] == {"faq": 2, "guide_page": 1}


def test_empty_chunk_list_does_not_crash():
    stats = chunk_stats([])
    assert stats["n_chunks"] == 0 and stats["n_tokens"]["median"] is None


def test_write_stats_keeps_other_strategies(tmp_path):
    write_stats("fixed@v1", {"n_chunks": 3}, tmp_path)
    write_stats("struct@v1", {"n_chunks": 9}, tmp_path)
    write_stats("fixed@v1", {"n_chunks": 4}, tmp_path)
    saved = json.loads((tmp_path / "chunk_stats.json").read_text())
    assert list(saved) == ["fixed@v1", "struct@v1"]
    assert saved["fixed@v1"]["n_chunks"] == 4 and saved["struct@v1"]["n_chunks"] == 9


def test_main_on_fixture_chunks_writes_json_and_png(tmp_path, capsys):
    chunks = FIX / "chunks" / "struct@v1.jsonl"
    rc = main(["--strategy", "struct", "--chunks", str(chunks), "--artifacts", str(tmp_path)])
    assert rc == 0
    saved = json.loads((tmp_path / "chunk_stats.json").read_text())
    assert saved["struct@v1"]["n_chunks"] == 9
    assert saved["struct@v1"]["n_docs"] == 5
    png = tmp_path / "chunk_hist_struct@v1.png"
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "struct@v1: 9 chunks from 5 docs" in capsys.readouterr().out


def test_missing_chunk_file_says_what_to_run(tmp_path):
    with pytest.raises(FileNotFoundError, match="make chunk"):
        read_chunk_file(tmp_path / "nope.jsonl")
