"""Report 02's tables come only from artifacts; prose outside the markers is untouched."""

import json

import pytest

from src.common import paths
from src.reports import chunking as rc

SWITCHES = {
    "closed_book": False, "retrieval_mode": "dense", "chunk_strategy": "fixed@v1",
    "rerank": False, "gate": False, "gate_threshold": None, "checks": [],
    "generator_model": None,
}  # fmt: skip
METRICS = {"recall@5": 0.5, "recall@20": 0.8, "mrr": 0.4, "ndcg@10": 0.45}


def stats(n):
    return {
        "n_chunks": n, "n_docs": 3, "chunks_per_doc_median": 2.0,
        "n_tokens": {"min": 10, "median": 300.0, "p95": 512.0, "max": 512, "mean": 280.5},
        "n_tokens_total": n * 280, "n_at_max_tokens": 1, "n_over_max_tokens": 0,
        "by_page_type": {"regulation_summary": n - 1, "guide_page": 1},
    }  # fmt: skip


def retrieval(strategy, recall5):
    return {
        "switches": SWITCHES | {"chunk_strategy": strategy},
        "n_scored": 30,
        "pool": METRICS,
        "final": METRICS | {"recall@5": recall5},
    }


@pytest.fixture
def artifacts(tmp_path):
    a = tmp_path / "artifacts"
    a.mkdir()
    (a / "chunk_stats.json").write_text(json.dumps({"fixed@v1": stats(7), "struct@v1": stats(9)}))
    (a / "chunk_hist_fixed@v1.png").write_bytes(b"png")
    (a / "embedding_info.json").write_text(json.dumps({
        "model": "BAAI/bge-small-en-v1.5", "hf_revision": "abc123", "dimension": 384,
        "distance": "cosine", "normalized": True, "sample_norm_min": 0.9999998,
        "sample_norm_max": 1.0000001, "query_instruction": "Q: ",
        "sentence_transformers": "6.1.0", "chromadb": "1.5.9", "strategy": "fixed@v1",
        "total": 7,
    }))  # fmt: skip
    (a / "migration.json").write_text(json.dumps({
        "from": "fixed@v1", "to": "struct@v1", "commit": "bb9c56a", "run_at": "2026-10-10T16:31:37",
        "n_docs": 3, "before": {"fixed@v1": 7}, "generated": 9, "deleted_old": 7,
        "deleted_stale_new": 0, "upserted": 9, "after": {"struct@v1": 9}, "bm25": "built",
        "orphan_check": {"passed": True, "index_minus_manifest": 0, "manifest_minus_index": 0,
                         "old_strategy_count": 0},
        "seconds": 0.1,
    }))  # fmt: skip
    (a / "retrieval_b2_dev.json").write_text(json.dumps(retrieval("fixed@v1", 0.5)))
    (a / "retrieval_a1_dev.json").write_text(json.dumps(retrieval("struct@v1", 0.6)))
    return a


def doc(*blocks):
    parts = ["# Title\n\nHand-written intro stays.\n"]
    for b in blocks:
        parts.append(f"<!-- BEGIN AUTO:{b} -->\nold content\n<!-- END AUTO:{b} -->\n")
    return "\n".join(parts) + "\nHand-written ending stays.\n"


def test_tables_are_filled_from_artifacts(artifacts):
    out = rc.render(doc(*rc.BLOCKS), artifacts)
    assert "old content" not in out
    assert "| `fixed@v1` | 7 | 3 | 2 | 10 | 300 | 512 | 512 | 280.5 | 0 |" in out
    assert "| `struct@v1` | 9 |" in out
    assert "| Dimension | 384 |" in out
    assert "| Measured vector norms (sample) | 0.9999998 to 1.0000001 |" in out
    assert "| Orphan check | **PASS** |" in out
    assert "| Recall@5 | 0.5 | 0.6 | +0.1000 |" in out
    assert "![Chunk length, fixed@v1](artifacts/chunk_hist_fixed@v1.png)" in out
    assert "Not produced yet: histogram for struct@v1" in out


def test_prose_outside_markers_is_kept_and_rendering_is_idempotent(artifacts):
    once = rc.render(doc("chunk_counts", "migration"), artifacts)
    assert "Hand-written intro stays." in once and "Hand-written ending stays." in once
    assert rc.render(once, artifacts) == once


def test_missing_artifacts_say_how_to_produce_them_and_never_show_numbers(tmp_path):
    out = rc.render(doc(*rc.BLOCKS), tmp_path)
    assert out.count("_Not produced yet") >= len(rc.BLOCKS)
    assert "make migrate FROM=fixed@v1 TO=struct@v1" in out
    assert "`make retrieval-eval CONFIG=b2 SPLIT=dev` and `make retrieval-eval" in out
    assert "| `fixed@v1` | – |" in out


def test_dev_metrics_refuse_runs_that_differ_in_more_than_chunking(artifacts):
    other = retrieval("struct@v1", 0.6)
    other["switches"]["rerank"] = True
    (artifacts / "retrieval_a1_dev.json").write_text(json.dumps(other))
    out = rc.render(doc("dev_metrics"), artifacts)
    assert "Cannot compare" in out and "rerank" in out


def test_unknown_or_unclosed_blocks_are_errors(artifacts):
    with pytest.raises(ValueError, match="unknown AUTO block"):
        rc.render(doc("nonsense"), artifacts)
    with pytest.raises(ValueError, match="unclosed"):
        rc.render("<!-- BEGIN AUTO:migration -->\nx\n", artifacts)


def test_the_real_report_has_every_block_and_renders(tmp_path):
    report = (paths.REPORTS / "02_chunking.md").read_text()
    for name in rc.BLOCKS:
        assert f"<!-- BEGIN AUTO:{name} -->" in report
    rc.render(report, tmp_path)  # no artifacts yet: must still render cleanly


def test_main_writes_the_report(artifacts, tmp_path, capsys):
    report = tmp_path / "02.md"
    report.write_text(doc("chunk_counts"))
    assert rc.main(["--report", str(report), "--artifacts", str(artifacts)]) == 0
    assert "| `struct@v1` | 9 |" in report.read_text()
    assert "0 block(s) still waiting" in capsys.readouterr().out
