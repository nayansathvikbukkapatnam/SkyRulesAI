"""Gate tuning: replay thresholds on a saved dev run and pick one by the stated rule."""

import json

import pytest

from src.eval import tune_gate


def item(id_, category, score, status, correct):
    return {
        "id": id_,
        "category": category,
        "answer": {"status": status, "trace": {"gate_score": score}},
        "metrics": {"correct": correct},
    }


ITEMS = [
    item("u1", "unanswerable", -6.0, "refused", True),
    item("w1", "ambiguous", -3.0, "answered", False),  # a wrong answer with a weak top passage
    item("c1", "single_hop", -1.0, "answered", True),  # the weakest correct answer
    item("c2", "single_hop", 4.0, "answered", True),
    item("r1", "temporal", 5.0, "refused", False),  # already an over-refusal
]


def test_replay_without_a_gate_keeps_the_saved_results():
    row = tune_gate.replay(ITEMS, None)
    assert (row["correct"], row["wrong_answers"], row["over_refusals"]) == (3, 1, 1)
    assert row["gated"] == [] and row["correct_refusal"] == 1.0


def test_a_gated_item_becomes_a_refusal():
    row = tune_gate.replay(ITEMS, -2.0)
    assert row["gated"] == ["u1", "w1"]
    assert (row["correct"], row["wrong_answers"], row["over_refusals"]) == (3, 0, 2)
    too_high = tune_gate.replay(ITEMS, 0.0)  # also stops the correct c1
    assert too_high["correct"] == 2


def test_candidates_are_gap_midpoints_plus_no_gate():
    thresholds = [t for t, _ in tune_gate.candidates(ITEMS)]
    assert thresholds == [None, -4.5, -2.0, 1.5, 4.5]


def test_choose_prefers_fewer_wrong_answers_then_the_widest_gap():
    chosen = tune_gate.choose(ITEMS)["chosen"]
    assert chosen["threshold"] == -2.0  # keeps c1, stops the wrong w1
    assert chosen["gap"] == 2.0


def test_main_writes_the_artifact_and_refuses_a_test_run(tmp_path):
    run = {"split": "dev", "git_commit": "abc1234", "items": ITEMS}
    (tmp_path / "eval_b3_dev.json").write_text(json.dumps(run))
    assert tune_gate.main(["--config", "b3", "--artifacts", str(tmp_path)]) == 0
    out = json.loads((tmp_path / "gate_tuning_dev.json").read_text())
    assert out["chosen"]["threshold"] == -2.0 and out["source_commit"] == "abc1234"
    (tmp_path / "eval_b3_dev.json").write_text(json.dumps({**run, "split": "test"}))
    with pytest.raises(SystemExit):
        tune_gate.main(["--config", "b3", "--artifacts", str(tmp_path)])
