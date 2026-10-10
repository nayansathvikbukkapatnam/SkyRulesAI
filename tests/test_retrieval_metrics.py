"""Retrieval metrics: hits need the right page AND the quote; recall, MRR and nDCG are exact.

The golden items here are synthetic test items over the fixture pages, written to a
temp folder. They are not golden-set items and never touch eval/golden.jsonl.
"""

import json
import math

import pytest

from src.eval import retrieval_metrics as rm
from src.eval.retrieval_metrics import evidence_pairs, hits, item_metrics, main, split_ids

BASE = "https://fixtures.skyrules.test"


def cand(cid, url, text):
    return {
        "chunk_id": cid, "text": text, "source_url": BASE + url, "title": "t",
        "effective_date": None, "page_type": "faq", "dense_score": None, "bm25_score": None,
        "fused_score": None,
    }  # fmt: skip


ALTITUDE = cand("a", "/uas/recreational_flyers", "Stay at or below 400 feet above ground level.")
OTHER = cand("b", "/uas/other", "Stay at or below 400 feet above ground level.")
NOISE = cand("c", "/uas/recreational_flyers", "Take the free online safety test.")
FEE = cand("d", "/uas/register", "| Drone use | Fee |\n| --- | --- |\n| Recreational | $5 |")

SINGLE = {
    "id": "t-1", "category": "single_hop", "question": "How high?",
    "source_url": BASE + "/uas/recreational_flyers/", "evidence_quote": "at or below 400 FEET",
}  # fmt: skip
MULTI = {
    "id": "t-2", "category": "multi_hop", "question": "Height and fee?",
    "source_url": [BASE + "/uas/recreational_flyers", BASE + "/uas/register"],
    "evidence_quote": ["400 feet above ground level", "Recreational $5"],
}  # fmt: skip
UNANSWERABLE = {"id": "t-3", "category": "unanswerable", "question": "Fly on Mars?"}


# --- Hits ----------------------------------------------------------------------


def test_hit_needs_the_same_page_and_the_quote():
    pairs = evidence_pairs(SINGLE)
    assert hits(ALTITUDE, pairs) == {0}  # trailing slash and case differ: still a hit
    assert hits(OTHER, pairs) == set()  # same words, wrong page
    assert hits(NOISE, pairs) == set()  # right page, no quote


def test_quote_from_a_table_row_matches_without_pipes():
    assert hits(FEE, evidence_pairs(MULTI)) == {1}


def test_items_without_evidence_have_no_pairs():
    assert evidence_pairs(UNANSWERABLE) == []
    assert evidence_pairs({"source_url": ["a", "b"], "evidence_quote": "one"}) == []


# --- Metrics -------------------------------------------------------------------


def test_single_hop_metrics():
    m = item_metrics([NOISE, OTHER, ALTITUDE], evidence_pairs(SINGLE))
    assert m["first_hit_rank"] == 3
    assert m["mrr"] == pytest.approx(1 / 3)
    assert m["recall@5"] == m["recall@20"] == 1.0
    assert m["ndcg@10"] == pytest.approx((1 / math.log2(4)) / 1.0)


def test_hit_after_rank_5_counts_only_for_recall_at_20():
    ranked = [NOISE] * 6 + [ALTITUDE]
    m = item_metrics(ranked, evidence_pairs(SINGLE))
    assert (m["recall@5"], m["recall@20"]) == (0.0, 1.0)


def test_multi_hop_partial_recall_and_ndcg():
    pairs = evidence_pairs(MULTI)
    m = item_metrics([ALTITUDE, NOISE], pairs)
    assert m["recall@5"] == 0.5
    both = item_metrics([FEE, NOISE, ALTITUDE], pairs)
    assert both["recall@5"] == 1.0
    idcg = 1 / math.log2(2) + 1 / math.log2(3)
    assert both["ndcg@10"] == pytest.approx((1 / math.log2(2) + 1 / math.log2(4)) / idcg)


def test_duplicate_hits_do_not_inflate_ndcg():
    m = item_metrics([ALTITUDE, ALTITUDE, ALTITUDE], evidence_pairs(SINGLE))
    assert m["ndcg@10"] == pytest.approx(1.0)


def test_no_hit_scores_zero():
    m = item_metrics([NOISE, OTHER], evidence_pairs(SINGLE))
    assert m == {"recall@5": 0.0, "recall@20": 0.0, "mrr": 0.0, "ndcg@10": 0.0,
                 "first_hit_rank": None}  # fmt: skip


# --- Split guard ---------------------------------------------------------------


def test_test_split_is_refused_without_final_and_frozen_tag(tmp_path, monkeypatch):
    split = tmp_path / "split.json"
    split.write_text(json.dumps({"dev": ["t-1"], "test": ["t-2"]}))
    with pytest.raises(SystemExit):
        split_ids("test", split, final=False)
    monkeypatch.setattr(rm, "frozen_tag_exists", lambda tag="b3-frozen": False)
    with pytest.raises(SystemExit):
        split_ids("test", split, final=True)
    assert split_ids("dev", split, final=False) == {"t-1"}


# --- End to end with a fake search ----------------------------------------------


def test_main_writes_artifact_with_pool_and_final(tmp_path, capsys):
    golden = tmp_path / "golden.jsonl"
    golden.write_text("".join(json.dumps(i) + "\n" for i in (SINGLE, MULTI, UNANSWERABLE)))
    split = tmp_path / "split.json"
    split.write_text(json.dumps({"dev": ["t-1", "t-2", "t-3"], "test": []}))

    def search(question):
        pool = [NOISE, OTHER, ALTITUDE, FEE]
        final = [ALTITUDE, FEE]  # as if a reranker promoted both hits
        return pool, final, {"retrieve": 12.0, "rerank": 30.0}

    args = ["--config", "a3", "--golden", str(golden), "--split-file", str(split),
            "--artifacts", str(tmp_path)]  # fmt: skip
    assert main(args, search) == 0
    result = json.loads((tmp_path / "retrieval_a3_dev.json").read_text())
    assert (result["n_scored"], result["n_skipped"]) == (2, 1)
    assert result["final"]["mrr"] == 1.0
    assert result["pool"]["mrr"] == pytest.approx(1 / 3, abs=1e-4)  # means are rounded to 4 dp
    assert result["pool_recall@20_ceiling"] == 1.0
    assert result["latency_ms"]["rerank"]["p50"] == 30.0
    assert result["switches"]["rerank"] is True
    assert "a3 on dev: 2 items scored (1 skipped)" in capsys.readouterr().out


def test_closed_book_config_has_nothing_to_score():
    from src.common.config import load_config

    with pytest.raises(SystemExit):
        rm.build_search(load_config("b0"))
