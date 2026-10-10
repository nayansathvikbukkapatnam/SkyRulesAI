"""Tests for src/eval/split.py. Golden sets here are checker test inputs, not real items."""

import json

import pytest

from src.eval import split as sp
from tests.test_golden_check import NORMALIZED, full_set, write_jsonl


@pytest.fixture
def golden(tmp_path):
    return write_jsonl(tmp_path / "golden.jsonl", full_set())


def test_split_is_60_40_and_stratified(golden, tmp_path):
    record = sp.run(golden, NORMALIZED, tmp_path / "split.json")
    assert record["n_items"] == 40
    assert (record["n_dev"], record["n_test"]) == (24, 16)
    assert set(record["dev"]).isdisjoint(record["test"])
    for cat, c in record["counts"].items():
        assert c["dev"] >= 1 and c["test"] >= 1, cat
    assert record["seed"] == 42


def test_split_is_deterministic(golden, tmp_path):
    a = sp.run(golden, NORMALIZED, tmp_path / "a.json")
    b = sp.run(golden, NORMALIZED, tmp_path / "b.json")
    assert (a["dev"], a["test"]) == (b["dev"], b["test"])


def test_refuses_to_overwrite(golden, tmp_path):
    out = tmp_path / "split.json"
    sp.run(golden, NORMALIZED, out)
    before = out.read_text()
    with pytest.raises(sp.SplitError, match="never overwritten"):
        sp.run(golden, NORMALIZED, out)
    assert out.read_text() == before
    assert sp.main(["--golden", str(golden), "--normalized", str(NORMALIZED), "--out", str(out)])


def test_refuses_a_golden_set_that_fails_the_checker(tmp_path):
    bad = write_jsonl(tmp_path / "golden.jsonl", full_set()[:10])
    with pytest.raises(sp.SplitError, match="golden-check"):
        sp.run(bad, NORMALIZED, tmp_path / "split.json")
    assert not (tmp_path / "split.json").exists()


def test_category_with_one_item_cannot_be_stratified():
    items = [{"id": f"s{i}", "category": "single_hop"} for i in range(5)]
    items.append({"id": "t0", "category": "temporal"})
    with pytest.raises(sp.SplitError, match="temporal"):
        sp.make_split(items)


def test_test_split_needs_final(golden, tmp_path):
    out = tmp_path / "split.json"
    record = sp.run(golden, NORMALIZED, out)
    assert sp.load_split("dev", split_file=out, golden=golden) == record["dev"]
    with pytest.raises(sp.SplitError, match="runs once"):
        sp.load_split("test", split_file=out, golden=golden)
    assert sp.load_split("test", final=True, split_file=out, golden=golden) == record["test"]


def test_edited_golden_set_is_detected(golden, tmp_path):
    out = tmp_path / "split.json"
    sp.run(golden, NORMALIZED, out)
    items = full_set()
    items[0]["gold_answer"] = "changed after the split"
    write_jsonl(golden, items)
    with pytest.raises(sp.SplitError, match="changed after the split"):
        sp.load_split("dev", split_file=out, golden=golden)


def test_main_reports_success(golden, tmp_path, capsys):
    out = tmp_path / "split.json"
    code = sp.main(["--golden", str(golden), "--normalized", str(NORMALIZED), "--out", str(out)])
    assert code == 0
    assert "24 dev, 16 test" in capsys.readouterr().out
    assert json.loads(out.read_text())["n_test"] == 16
