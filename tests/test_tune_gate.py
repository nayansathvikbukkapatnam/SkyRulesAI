"""Gate tuning: replays thresholds on a dev eval run and picks the best, dev only."""

import json
import math

import pytest

from src.eval.tune_gate import main, simulate, tune


def rec(cat, answered, correct, score):
    return {
        "category": cat,
        "answer": {"trace": {"gate_score": score}},
        "metrics": {"answered": answered, "correct": correct},
    }


# Two answerable items answered correctly with strong evidence, one answerable item
# answered WRONG on weak evidence, and two unanswerable items the model answered anyway
# on weak evidence (hallucinations). A gate between the weak and strong scores helps.
RECORDS = [
    rec("single_hop", True, True, 6.0),
    rec("single_hop", True, True, 4.0),
    rec("multi_hop", True, False, -2.0),
    rec("unanswerable", True, False, -5.0),
    rec("unanswerable", True, False, -1.0),
]


def test_simulate_without_gate_matches_the_run():
    s = simulate(RECORDS, -math.inf)
    assert s == {"threshold": -math.inf, "correctness": 0.4, "over_refusal": 0.0,
                 "correct_refusal": 0.0, "newly_gated": 0}  # fmt: skip


def test_simulate_with_a_gate_refuses_weak_evidence():
    s = simulate(RECORDS, 0.0)
    assert s["correctness"] == 0.8  # both strong answers kept, both unanswerables refused
    # the weak wrong answer is now refused too
    assert s["over_refusal"] == pytest.approx(1 / 3, abs=1e-4)
    assert s["correct_refusal"] == 1.0
    assert s["newly_gated"] == 3


def test_tune_picks_the_lowest_threshold_with_best_correctness():
    result = tune(RECORDS)
    assert result["chosen"]["correctness"] == 0.8
    # Refusing all three weak items needs a threshold in (-1, 4]; the tuner takes the
    # midpoint of that gap between observed scores, 1.5.
    assert result["chosen"]["threshold"] == 1.5
    assert result["no_gate"]["correctness"] == 0.4


def test_missing_score_counts_as_gated():
    s = simulate([rec("single_hop", True, True, None)], 0.0)
    assert s["over_refusal"] == 1.0


def test_ungraded_runs_and_runs_without_scores_are_rejected():
    with pytest.raises(ValueError, match="ungraded"):
        tune([rec("single_hop", True, None, 1.0)])
    with pytest.raises(ValueError, match="rerank"):
        tune([rec("single_hop", True, True, None)])


def write_run(tmp_path, split="dev", gate=False):
    run = {"split": split, "git_commit": "abc1234", "switches": {"gate": gate, "rerank": True},
           "items": RECORDS}  # fmt: skip
    (tmp_path / f"eval_a3_{split}.json").write_text(json.dumps(run))


def test_main_writes_strict_json_and_plot(tmp_path, capsys):
    write_run(tmp_path)
    assert main(["--artifacts", str(tmp_path)]) == 0
    text = (tmp_path / "gate_tuning_dev.json").read_text()
    assert "Infinity" not in text  # strict JSON
    saved = json.loads(text)
    assert saved["chosen"]["threshold"] == 1.5 and saved["curve"][0]["threshold"] == "-inf"
    assert saved["source_git_commit"] == "abc1234"
    assert (tmp_path / "gate_tuning_dev.png").read_bytes()[:4] == b"\x89PNG"
    assert "chosen gate_threshold 1.5" in capsys.readouterr().out


def test_main_refuses_non_dev_and_gated_runs(tmp_path):
    (tmp_path / "eval_a3_dev.json").write_text(
        json.dumps({"split": "test", "switches": {"gate": False}, "items": RECORDS})
    )
    with pytest.raises(SystemExit, match="dev only"):
        main(["--artifacts", str(tmp_path)])
    write_run(tmp_path, gate=True)
    with pytest.raises(SystemExit, match="already gates"):
        main(["--artifacts", str(tmp_path)])
