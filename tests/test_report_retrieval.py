"""Report 04: tables are filled only from artifacts; missing artifacts say how to make them."""

import json

import pytest

from src.common.config import load_config, switches
from src.eval.retrieval_metrics import bootstrap_ci
from src.reports import retrieval as rr

REPORT = rr.REPORT


def run_artifact(config, values):
    """A retrieval_<config>_dev.json with per-item final/pool metrics set to `values`."""
    items = [{"id": f"t{i}", "final": {"recall@5": v, "mrr": v, "ndcg@10": v},
              "pool": {"recall@20": 1.0}} for i, v in enumerate(values)]  # fmt: skip
    mean = round(sum(values) / len(values), 4)
    return {
        "config": config,
        "switches": switches(load_config(config)),
        "n_scored": len(values),
        "final": {"recall@5": mean, "mrr": mean, "ndcg@10": mean},
        "pool": {"recall@20": 1.0},
        "pool_recall@20_ceiling": 1.0,
        "ci95": {"final": {"recall@5": bootstrap_ci(values)}, "pool": {"recall@20": [1.0, 1.0]}},
        "latency_ms": {
            "retrieve": {"p50": 40.0, "p95": 80.0},
            "rerank": {"p50": 900.0, "p95": 1500.0},
        },
        "items": items,
    }


@pytest.fixture
def artifacts(tmp_path):
    for config, values in {"b1": [0, 1, 0, 1], "b2": [1, 1, 0, 0], "a1": [1, 1, 1, 0],
                           "a2": [0, 0, 1, 1], "a3": [1, 1, 1, 1]}.items():  # fmt: skip
        (tmp_path / f"retrieval_{config}_dev.json").write_text(
            json.dumps(run_artifact(config, values))
        )
    return tmp_path


def test_bootstrap_ci_brackets_the_mean_and_is_reproducible():
    values = [0, 1] * 20
    lo, hi = bootstrap_ci(values)
    assert lo < 0.5 < hi
    assert bootstrap_ci(values) == [lo, hi]
    assert bootstrap_ci([1.0, 1.0]) == [1.0, 1.0]
    assert bootstrap_ci([]) is None


def test_paired_diff_uses_the_same_items():
    before, after = run_artifact("a2", [0, 0, 1, 1]), run_artifact("a3", [1, 1, 1, 1])
    mean, ci = rr.paired_diff_ci(before, after, "final", "recall@5")
    assert mean == 0.5 and ci[0] <= 0.5 <= ci[1]


def test_report_template_fills_every_block(artifacts, tmp_path):
    (artifacts / "filter_demo.json").write_text(json.dumps({
        "strategy": "struct@v1", "git_commit": "abc1234",
        "cases": [{"query": "q", "page_type": "faq", "all_match": True,
                   "top": [{"chunk_id": "x", "page_type": "faq", "title": "T"}]}]}))  # fmt: skip
    (artifacts / "gate_tuning_dev.json").write_text(json.dumps({
        "source": "eval_a3_dev.json", "source_git_commit": "abc1234", "n_items": 30,
        "rule": "maximize dev correctness",
        "chosen": {"threshold": 1.5, "correctness": 0.7, "over_refusal": 0.1,
                   "correct_refusal": 0.9},
        "no_gate": {"threshold": "-inf", "correctness": 0.6, "over_refusal": 0.0,
                    "correct_refusal": 0.5}}))  # fmt: skip
    text = rr.render(REPORT.read_text(), artifacts)
    assert "_Not produced yet" not in text
    assert "| A3 | hybrid | `struct@v1` | yes |" in text
    assert "+0.5" in text  # A2 -> A3 paired change
    assert "p50 900 ms" in text
    assert "Chosen `gate_threshold`: **1.5**" in text
    assert "all results match" in text


def test_missing_artifacts_say_which_command_to_run(tmp_path):
    text = rr.render(REPORT.read_text(), tmp_path)
    assert "make retrieval-eval CONFIG=a3 SPLIT=dev" in text
    assert "python -m src.eval.tune_gate --config a3" in text
    assert "--demo-filter" in text


def test_render_is_idempotent(artifacts):
    once = rr.render(REPORT.read_text(), artifacts)
    assert rr.render(once, artifacts) == once


def test_filter_demo_flags_a_leak():
    def leaky(query, k, mode, filters, strategy):
        return [{"chunk_id": "a", "page_type": filters["page_type"], "title": "ok"},
                {"chunk_id": "b", "page_type": "other", "title": "leak"}]  # fmt: skip

    demo = rr.demo_filter(leaky)
    assert demo["cases"] and not any(c["all_match"] for c in demo["cases"])
