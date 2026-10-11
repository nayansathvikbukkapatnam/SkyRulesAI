"""Failure sheet and label counts, on a fixture eval run. Labels here are test data."""

import json

import pytest

from src.eval import failure_labels as fl


def record(item_id, error=None, **metrics):
    base = {"correct": True, "refused_correctly": None, "over_refused": None, "judge": None}
    base.update(metrics)
    return {
        "id": item_id,
        "category": "single_hop",
        "question": f"question {item_id}",
        "error": error,
        "retrieved_chunk_ids": [f"c{n}" for n in range(8)],
        "answer": {
            "status": "answered",
            "sentences": [{"text": "A sentence.", "chunk_ids": ["c0"]}],
            "citations": [
                {"n": 1, "chunk_id": "c0", "url": "https://example.test/a", "title": "t"}
            ],
            "trace": {"refusal_reason": None},
        },
        "metrics": base,
    }


@pytest.fixture
def eval_files(tmp_path):
    items = [
        record("ok"),
        record("wrong", correct=False, judge={"correct": False, "reason": "says 500 ft"}),
        record("overref", correct=False, over_refused=True),
        record("unans", correct=False, refused_correctly=False),
        record("crash", error="RuntimeError: index missing", correct=False),
    ]
    eval_path = tmp_path / "eval_b3_dev.json"
    eval_path.write_text(json.dumps({"config": "b3", "split": "dev", "items": items}))
    golden = tmp_path / "golden.jsonl"
    golden.write_text(
        json.dumps({"id": "wrong", "gold_answer": "400 feet", "source_url": "u"}) + "\n"
    )
    return eval_path, golden, tmp_path / "failure_labels_dev.jsonl"


def test_failure_types():
    assert fl.failure_type(record("x")) is None
    assert fl.failure_type(record("x", correct=False)) == "incorrect_answer"
    assert fl.failure_type(record("x", over_refused=True)) == "over_refusal"
    assert fl.failure_type(record("x", refused_correctly=False)) == "answered_unanswerable"
    assert fl.failure_type(record("x", error="boom")) == "error"


def test_sheet_lists_only_failures_with_context_and_empty_labels(eval_files):
    eval_path, golden, out = eval_files
    lines = fl.make_sheet({"b3": eval_path}, golden, out, "dev")
    assert [line["id"] for line in lines] == ["wrong", "overref", "unans", "crash"]
    wrong = lines[0]
    assert wrong["gold_answer"] == "400 feet" and wrong["judge_reason"] == "says 500 ft"
    assert wrong["top_chunk_ids"] == ["c0", "c1", "c2", "c3", "c4"]
    assert wrong["citations"][0]["chunk_id"] == "c0"
    assert all(line["label"] == "" and line["labelled_by"] == "" for line in lines)
    assert len(fl.read_jsonl(out)) == 4


def test_sheet_refuses_to_overwrite_human_labels(eval_files):
    eval_path, golden, out = eval_files
    lines = fl.make_sheet({"b3": eval_path}, golden, out, "dev")
    lines[0].update(label="hallucination", labelled_by="Tester")
    out.write_text("".join(json.dumps(line) + "\n" for line in lines))
    with pytest.raises(SystemExit):
        fl.make_sheet({"b3": eval_path}, golden, out, "dev")
    fl.make_sheet({"b3": eval_path}, golden, out, "dev", force=True)  # only when asked


def test_sheet_can_combine_several_runs(eval_files):
    eval_path, golden, out = eval_files
    lines = fl.make_sheet({"b3": eval_path, "b2": eval_path}, golden, out, "dev")
    assert [line["config"] for line in lines] == ["b3"] * 4 + ["b2"] * 4


def test_count_validates_labels_and_names():
    good = [
        {"id": "a", "label": "retrieval_miss", "labelled_by": "Tester", "config": "b3"},
        {"id": "b", "label": "hallucination", "labelled_by": "Tester", "config": "b3"},
        {"id": "c", "label": "", "labelled_by": "", "config": "b3"},
    ]
    result = fl.count_labels(good)
    assert result["total"] == 2 and result["unlabelled"] == 1
    assert result["counts"]["retrieval_miss"] == 1 and result["counts"]["over_refusal"] == 0
    assert set(result["counts"]) == set(fl.LABELS)
    assert result["meets_minimum"] is False and result["labelled_by"] == ["Tester"]
    assert result["configs"] == ["b3"] and result["by_config"] == {"b3": 2}
    with pytest.raises(ValueError, match="not one of"):
        fl.count_labels([{"id": "a", "label": "bad_retrieval", "labelled_by": "Tester"}])
    with pytest.raises(ValueError, match="labelled_by is empty"):
        fl.count_labels([{"id": "a", "label": "hallucination", "labelled_by": ""}])


def test_fifteen_labels_meet_the_minimum():
    lines = [
        {"id": str(n), "label": fl.LABELS[n % len(fl.LABELS)], "labelled_by": "T", "config": "b3"}
        for n in range(15)
    ]
    assert fl.count_labels(lines)["meets_minimum"] is True


def test_allowed_labels_match_deliverables_section_4():
    assert fl.LABELS == (
        "retrieval_miss", "rank_failure", "chunk_boundary", "extraction_failure", "stale_content",
        "reranker_demotion", "generator_ignored_context", "invalid_citation", "over_refusal",
        "hallucination",
    )  # fmt: skip
