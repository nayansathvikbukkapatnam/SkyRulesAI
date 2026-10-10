"""Report 05 tooling on fake eval files: tables, refusal reasons, the blind label sheet
and judge agreement. No models, no network."""

import json

import pytest

from src.reports import generation as gen

SUMMARY = {
    "n_items": 4,
    "n_errors": 0,
    "n_answered": 2,
    "correctness": {"rate": 0.75, "n": 4},
    "correctness_by_category": {},
    "citation_validity": {"rate": 1.0, "n": 2},
    "correct_refusal": {"rate": 1.0, "n": 1},
    "over_refusal": {"rate": 0.3333, "n": 3},
    "latency_ms": {s: {"p50": 100.0, "p95": 250.5} for s in gen.STAGES},
    "cost_usd_per_query": 0.00042,
    "cost_usd_total": 0.00168,
}


def answered(text, judge_ok, tag="normal", referral=None):
    return {
        "answer": {
            "status": "answered",
            "sentences": [{"text": text, "chunk_ids": ["c1"]}],
            "router_tag": tag,
            "referral": referral,
            "trace": {
                "refusal_reason": None,
                "checks": {
                    "ok": True,
                    "requested": ["citation_validity", "quote_limit"],
                    "per_check": {"citation_validity": True, "quote_limit": True},
                    "sentences": [{"citation_validity": {"ok": True}, "quote_limit": {"ok": True}}],
                },
            },
        },
        "metrics": {"judge": {"correct": judge_ok, "reason": "r"}, "correct": judge_ok},
    }


def refused(reason, checks=None, tag="normal", referral=None):
    return {
        "answer": {
            "status": "refused",
            "sentences": [],
            "router_tag": tag,
            "referral": referral,
            "trace": {"refusal_reason": reason, "checks": checks or {}},
        },
        "metrics": {"judge": None, "correct": False},
    }


def eval_run(config="b3", split="dev"):
    location = {"kind": "location", "text": "Check B4UFLY."}
    items = [
        {"id": "g001", "category": "single_hop", "question": "How high?", "error": None,
         **answered("Stay at or below 400 feet.", True)},
        {"id": "g002", "category": "single_hop", "question": "Register?", "error": None,
         **answered("Register drones over 0.55 pounds.", False)},
        {"id": "g003", "category": "unanswerable", "question": "Which drone to buy?", "error": None,
         **refused("not enough support in the passages")},
        {"id": "g004", "category": "comparative", "question": "San Diego tonight?", "error": None,
         **refused("check failed: Quotes", tag="location_live", referral=location,
                   checks={"requested": ["quote_limit"], "per_check": {"quote_limit": False},
                           "sentences": [{"quote_limit": {"ok": False}}]})},
    ]  # fmt: skip
    return {
        "config": config,
        "split": split,
        "git_commit": "abc1234",
        "summary": SUMMARY,
        "items": items,
    }


@pytest.fixture
def artifacts(tmp_path):
    for config in ("b2", "b3"):
        (tmp_path / f"eval_{config}_dev.json").write_text(json.dumps(eval_run(config)))
    return tmp_path


def report_text():
    return "\n".join(f"<!-- BEGIN AUTO:{b} -->\nold\n<!-- END AUTO:{b} -->" for b in gen.BLOCKS)


# --- Tables ----------------------------------------------------------------------------------


def test_rates_table_lists_each_config_with_its_rates(artifacts):
    out = gen.block_rates(artifacts, "dev")
    assert "| B2 | 4 | 2 | 0 | 0.75 (n=4) | 1 (n=2) | 1 (n=1) | 0.3333 (n=3) |" in out
    assert "| B3 |" in out and "B3 abc1234" in out


def test_refusal_reasons_are_grouped_by_kind_and_category(artifacts):
    out = gen.block_refusals(artifacts, "dev")
    assert "| Not enough support (model said insufficient) | 1 | 1 | 0 |" in out
    assert "| Failed check: Quotes | 1 | 0 | 1 |" in out


def test_checks_table_counts_failed_answers_and_sentences(artifacts):
    out = gen.block_checks(artifacts, "dev")
    assert "| Quotes (25-word limit) | 3 | 1 | 1 |" in out
    assert "| Citations | 2 | 0 | 0 |" in out


def test_referrals_include_refused_location_questions(artifacts):
    out = gen.block_referrals(artifacts, "dev")
    assert "| location_live | location | refused | 1 |" in out


def test_latency_and_cost_table(artifacts):
    out = gen.block_latency_cost(artifacts, "dev")
    assert "100 / 250.5" in out and "0.00042" in out and "0.00168" in out


def test_settings_show_temperature_not_sent():
    out = gen.block_settings(None, "dev")
    assert "| temperature | null (not sent) |" in out
    assert "| thinking | disabled |" in out


def test_missing_artifacts_say_not_produced_yet(tmp_path):
    for name, block in gen.BLOCKS.items():
        if name == "settings":
            continue
        assert "Not produced yet" in block(tmp_path, "dev") or "No " in block(tmp_path, "dev")


def test_render_fills_every_block_and_uses_no_dashes(artifacts):
    out = gen.render(report_text(), artifacts, "dev")
    assert "\nold\n" not in out
    assert "—" not in out and "–" not in out


# --- Blind label sheet and judge agreement ----------------------------------------------------


GOLDEN = {
    "g001": {"gold_answer": "At or below 400 feet.", "source_url": "https://example.test/a"},
    "g002": {"gold_answer": "Over 0.55 pounds.", "source_url": "https://example.test/b"},
}


def test_label_sheet_is_blind_and_only_has_graded_answers():
    sheet = gen.label_sheet(eval_run(), GOLDEN, n=20)
    assert [(r["config"], r["id"]) for r in sheet] == [("b3", "g001"), ("b3", "g002")]
    # refusals and unanswerables are left out
    assert sheet[0]["gold_answer"] == "At or below 400 feet."
    assert sheet[0]["system_answer"] == "Stay at or below 400 feet."
    for row in sheet:
        assert row["human_correct"] is None and row["labeled_by"] == ""
        assert "judge" not in json.dumps(row)  # the judge's verdict is never shown


def test_label_sheet_sample_is_reproducible():
    run = eval_run()
    run["items"] = [
        {**it, "id": f"g{i:03d}"} for i, it in enumerate(run["items"][:2] * 20, start=1)
    ]
    first = gen.label_sheet(run, {}, n=5)
    assert [r["id"] for r in first] == [r["id"] for r in gen.label_sheet(run, {}, n=5)]
    assert len(first) == 5


def labels(*pairs):
    return [{"id": i, "human_correct": h, "labeled_by": "Pavan"} for i, h in pairs]


def test_agreement_matrix_and_kappa():
    result = gen.agreement(eval_run(), labels(("g001", True), ("g002", True)))
    assert result["n"] == 2 and result["agreement"] == 0.5
    assert result["matrix"] == {
        "judge_yes_human_yes": 1,
        "judge_yes_human_no": 0,
        "judge_no_human_yes": 1,
        "judge_no_human_no": 0,
    }
    assert result["disagreements"] == ["b3:g002"]
    assert result["per_config"] == {"b3": 2}
    assert result["labeled_by"] == ["Pavan"]


def test_perfect_agreement_has_kappa_one():
    result = gen.agreement(eval_run(), labels(("g001", True), ("g002", False)))
    assert result["agreement"] == 1.0 and result["kappa"] == 1.0


@pytest.mark.parametrize(
    "bad",
    [
        [{"id": "g001", "human_correct": None, "labeled_by": "Pavan"}],
        [{"id": "g001", "human_correct": True, "labeled_by": ""}],
        [{"id": "g003", "human_correct": True, "labeled_by": "Pavan"}],  # judge never graded it
    ],
    ids=["unlabelled", "no-name", "no-judge-verdict"],
)
def test_agreement_refuses_incomplete_labels(bad):
    with pytest.raises(ValueError, match="not ready"):
        gen.agreement(eval_run(), bad)


def test_cli_writes_sheet_refuses_overwrite_and_test_split(artifacts, tmp_path):
    golden = tmp_path / "golden.jsonl"
    golden.write_text("".join(json.dumps({"id": k, **v}) + "\n" for k, v in GOLDEN.items()))
    sheet = tmp_path / "labels.jsonl"
    args = ["--artifacts", str(artifacts), "--golden", str(golden), "--labels", str(sheet)]
    assert gen.main(["label-sheet", *args]) == 0
    assert len(sheet.read_text().splitlines()) == 4  # B3's 2 graded answers, topped up from B2
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        gen.main(["label-sheet", *args])
    with pytest.raises(SystemExit, match="dev split only"):
        gen.main(["label-sheet", "--split", "test", *args])

    rows = [
        dict(json.loads(line), human_correct=True, labeled_by="Pavan")
        for line in sheet.read_text().splitlines()
    ]
    sheet.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert gen.main(["agreement", *args]) == 0
    written = json.loads((artifacts / "judge_agreement_dev.json").read_text())
    assert written["n"] == 4 and written["per_config"] == {"b3": 2, "b2": 2}
    assert "Agreement" in gen.block_judge(artifacts, "dev")


def test_sheet_tops_up_from_the_next_config_when_b3_has_too_few():
    runs = {"b3": eval_run("b3"), "b2": eval_run("b2")}
    sheet = gen.label_sheet(runs, GOLDEN, n=3)
    assert [r["config"] for r in sheet] == ["b3", "b3", "b2"]  # B3's 2 graded answers, then B2
    assert len({(r["config"], r["id"]) for r in sheet}) == 3


def test_agreement_matches_labels_by_config_and_id():
    b2 = eval_run("b2")
    b2["items"][0]["metrics"]["judge"] = {"correct": False, "reason": "r"}  # differs from B3
    runs = {"b3": eval_run("b3"), "b2": b2}
    labs = [
        {"config": "b3", "id": "g001", "human_correct": True, "labeled_by": "Pavan"},
        {"config": "b2", "id": "g001", "human_correct": False, "labeled_by": "Pavan"},
    ]
    result = gen.agreement(runs, labs)
    assert result["agreement"] == 1.0 and result["per_config"] == {"b3": 1, "b2": 1}


def test_cli_warns_when_fewer_than_n_graded_answers(artifacts, tmp_path, capsys):
    args = ["--artifacts", str(artifacts), "--golden", str(tmp_path / "none.jsonl"),
            "--labels", str(tmp_path / "l.jsonl"), "--configs", "b3"]  # fmt: skip
    (tmp_path / "none.jsonl").write_text("")
    gen.main(["label-sheet", *args])
    assert "WARNING: only 2 graded answers" in capsys.readouterr().out
