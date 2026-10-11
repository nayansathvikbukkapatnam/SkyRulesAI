"""Bootstrap intervals, McNemar tests and the B0 subset, on small fixture eval files."""

import json
import random

import pytest

from src.eval import stats


def item(item_id, correct, category="single_hop", **metrics):
    base = {
        "correct": correct,
        "citation_valid": None,
        "refused_correctly": None,
        "over_refused": None,
    }
    base.update(metrics)
    return {"id": item_id, "category": category, "question": "q", "error": None, "metrics": base}


def run(config, items, split="dev"):
    return {
        "config": config,
        "split": split,
        "switches": {"rerank": config == "b3"},
        "git_commit": "abc1234",
        "summary": {
            "latency_ms": {"total": {"p50": 900.0, "p95": 2100.0}},
            "cost_usd_per_query": 0.001,
        },
        "items": items,
    }


def write_runs(tmp_path, runs, split="dev"):
    for config, r in runs.items():
        (tmp_path / f"eval_{config}_{split}.json").write_text(json.dumps(r))


# --- Bootstrap -------------------------------------------------------------------------


def test_bootstrap_interval_contains_the_point_estimate_and_is_reproducible():
    values = [True] * 14 + [False] * 6
    r = stats.bootstrap_rate(values)
    assert r["rate"] == 0.7 and r["n"] == 20
    assert r["low"] <= r["rate"] <= r["high"]
    assert 0 <= r["low"] < r["high"] <= 1
    assert stats.bootstrap_rate(values) == r  # seed 42: same numbers every run


def test_none_values_are_left_out_and_n_reported():
    r = run("b2", [item("a", True), item("b", None), item("c", False)])
    assert stats.graded(r, "correct") == [True, False]
    assert stats.bootstrap_rate([])["rate"] is None


# --- Paired comparison and McNemar ------------------------------------------------------


def test_mcnemar_on_a_known_table():
    # 5 both correct, 1 only before, 9 only after, 5 neither.
    before = [True] * 5 + [True] * 1 + [False] * 9 + [False] * 5
    after = [True] * 5 + [False] * 1 + [True] * 9 + [False] * 5
    a = run("b2", [item(f"i{n}", v) for n, v in enumerate(before)])
    b = run("b3", [item(f"i{n}", v) for n, v in enumerate(after)])
    c = stats.paired_comparison(a, b)
    assert c["table"] == [[5, 1], [9, 5]]
    # Exact McNemar: two-sided binomial on 10 discordant items, 1 vs 9.
    assert c["p"] == pytest.approx(2 * 11 / 1024, abs=1e-6)
    assert c["difference"] == pytest.approx(0.4)
    assert c["low"] <= c["difference"] <= c["high"]


def test_identical_runs_give_no_difference_and_p_of_one():
    items = [item(f"i{n}", n % 3 != 0) for n in range(12)]
    c = stats.paired_comparison(run("b2", items), run("b3", items))
    assert c["difference"] == 0 and c["p"] == 1.0
    assert c["low"] == 0 and c["high"] == 0


def test_pairing_is_by_id_not_by_order():
    items = [item(f"i{n}", n % 2 == 0) for n in range(10)]
    after = [item(f"i{n}", True) for n in range(10)]
    shuffled = after[:]
    random.Random(1).shuffle(shuffled)
    assert stats.paired_comparison(run("b2", items), run("b3", after)) == stats.paired_comparison(
        run("b2", items), run("b3", shuffled)
    )


def test_pairs_only_items_both_runs_graded():
    a = run("b2", [item("x", True), item("y", None), item("z", False)])
    b = run("b3", [item("x", True), item("y", True), item("w", True)])
    assert stats.paired_comparison(a, b)["n"] == 1  # only "x" is graded in both


# --- B0 leakage ------------------------------------------------------------------------


def test_b0_incorrect_subset_and_each_configs_score_on_it():
    runs = {
        "b0": run("b0", [item("a", False), item("b", True), item("c", False), item("d", None)]),
        "b3": run("b3", [item("a", True), item("b", True), item("c", False), item("d", True)]),
    }
    leak = stats.b0_incorrect_subset(runs)
    assert leak["ids"] == ["a", "c"] and leak["n"] == 2
    assert leak["correctness"]["b3"]["rate"] == 0.5
    assert leak["correctness"]["b0"]["rate"] == 0.0
    assert stats.b0_incorrect_subset({"b3": runs["b3"]}) is None


# --- Whole run, report and split guard ---------------------------------------------------


def test_main_writes_stats_and_lists_missing_configs(tmp_path):
    items = [item(f"i{n}", n % 2 == 0, citation_valid=True) for n in range(8)]
    write_runs(tmp_path, {"b2": run("b2", items), "b3": run("b3", items)})
    assert stats.main(["--split", "dev", "--artifacts", str(tmp_path)]) == 0
    out = json.loads((tmp_path / "stats_dev.json").read_text())
    assert set(out["per_config"]) == {"b2", "b3"}
    assert out["missing_configs"] == ["b0", "b1", "a1", "a2", "a3"]
    assert out["inputs"]["b3"]["git_commit"] == "abc1234"
    assert out["method"]["resamples"] == 10_000 and out["method"]["seed"] == 42
    ablation = {(c["before"], c["after"]): c for c in out["comparisons"]}
    assert ablation[("b2", "a1")]["missing"] == ["a1"]
    assert ablation[("b2", "b3")]["n"] == 8
    assert out["per_config"]["b3"]["citation_validity"]["rate"] == 1.0


def test_main_without_eval_files_fails_cleanly(tmp_path):
    assert stats.main(["--split", "dev", "--artifacts", str(tmp_path)]) == 1


def test_report_tables_are_filled_from_stats_only(tmp_path):
    items = [item(f"i{n}", n % 2 == 0) for n in range(6)]
    runs = {"b0": run("b0", items), "b3": run("b3", [item(f"i{n}", True) for n in range(6)])}
    computed = stats.compute(runs, "dev")
    text = (
        "# 06\n<!-- BEGIN AUTO:run_info -->\n<!-- END AUTO:run_info -->\n"
        "<!-- BEGIN AUTO:baselines -->\n<!-- END AUTO:baselines -->\n"
        "<!-- BEGIN AUTO:leakage -->\n<!-- END AUTO:leakage -->\n"
        "<!-- BEGIN AUTO:failures -->\n<!-- END AUTO:failures -->\n"
    )
    out = stats.render_report(text, computed, "dev", tmp_path)
    assert "| B3 | `reports/artifacts/eval_b3_dev.json` | `abc1234` |" in out
    assert "| B3 | 1.000 [1.000, 1.000] (n=6)" in out
    assert "| B1 | not run |" in out
    assert "Items closed-book B0 got wrong: 3." in out
    assert "_Not produced yet: failure labels." in out


def test_test_split_is_refused_without_the_frozen_tag(monkeypatch):
    monkeypatch.setattr(stats, "frozen_tag_exists", lambda: False)
    with pytest.raises(SystemExit):
        stats.check_split("test", final=True)
    monkeypatch.setattr(stats, "frozen_tag_exists", lambda: True)
    with pytest.raises(SystemExit):
        stats.check_split("test", final=False)  # tag alone is not enough: FINAL=1 too
    stats.check_split("test", final=True)
    stats.check_split("dev", final=False)


def test_correctness_by_category_and_its_report_table(tmp_path):
    items = [
        item("a", True, category="single_hop"),
        item("b", False, category="single_hop"),
        item("c", True, category="multi_hop"),
        item("d", None, category="multi_hop"),  # not graded: left out
    ]
    runs = {"b3": run("b3", items)}
    per = stats.by_category(runs)["b3"]
    assert per["single_hop"]["rate"] == 0.5 and per["single_hop"]["n"] == 2
    assert per["multi_hop"]["n"] == 1 and per["temporal"]["n"] == 0
    text = "<!-- BEGIN AUTO:by_category -->\n<!-- END AUTO:by_category -->"
    out = stats.render_report(text, stats.compute(runs, "dev"), "dev", tmp_path)
    assert "| single_hop | 2 | 0.500 [" in out
    assert "| temporal | 0 | - |" in out
