"""Tests for src/eval/golden_check.py, using the fixture pages as the saved corpus.

The items below are test inputs for the checker, not golden set items.
"""

import json

import pytest

from src.common import paths
from src.eval import golden_check as gc

NORMALIZED = paths.FIXTURES / "normalized"
REMOTE_ID = "https://fixtures.skyrules.test/uas/getting_started/remote_id"
REGISTER = "https://fixtures.skyrules.test/uas/getting_started/register"
PART107 = "https://fixtures.skyrules.test/uas/commercial_operators"
STUDY = "https://fixtures.skyrules.test/sites/faa.gov/files/uas/fixture_study_guide.pdf"


def item(**overrides):
    base = {
        "id": "t-001",
        "category": "single_hop",
        "question": "How long does registration last?",
        "gold_answer": "Three years.",
        "source_url": REGISTER,
        "evidence_quote": "Registration lasts 3 years.",
        "author": "Alice",
        "verified_by": "Bob",
        "verified_date": "2026-10-10",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def errors_for(it):
    errors, _ = gc.check_item(it, gc.PageStore(NORMALIZED))
    return errors


def test_valid_item_passes():
    assert errors_for(item()) == []


def test_quote_match_ignores_case_whitespace_and_curly_quotes():
    assert errors_for(item(evidence_quote="  REGISTRATION   lasts\n3 years.")) == []
    assert gc.normalize_text("it’s “ok”") == gc.normalize_text("It's \"ok\"")


def test_quote_not_on_page_fails():
    errs = errors_for(item(evidence_quote="Registration lasts 5 years."))
    assert any("not found verbatim" in e for e in errs)


def test_paraphrased_quote_fails():
    errs = errors_for(item(evidence_quote="Registration is valid for three years."))
    assert any("not found verbatim" in e for e in errs)


def test_quote_over_25_words_fails():
    long_quote = " ".join(["word"] * 26)
    errs = errors_for(item(evidence_quote=long_quote))
    assert any("26 words" in e for e in errs)


def test_quote_can_match_a_table_row():
    assert errors_for(item(source_url=STUDY, evidence_quote="Stable air")) == []


def test_page_missing_from_corpus_fails():
    errs = errors_for(item(source_url="https://fixtures.skyrules.test/uas/not_crawled"))
    assert any("not in the saved corpus" in e for e in errs)


def test_verifier_must_differ_from_author():
    errs = errors_for(item(author="Alice", verified_by=" alice "))
    assert any("different person" in e for e in errs)


def test_missing_verification_fails():
    errs = errors_for(item(verified_by="", verified_date=None))
    assert any("'verified_by'" in e for e in errs)
    assert any("'verified_date'" in e for e in errs)


def test_bad_verified_date_fails():
    assert any("YYYY-MM-DD" in e for e in errors_for(item(verified_date="10/10/2026")))


def test_unknown_category_fails():
    assert any("unknown category" in e for e in errors_for(item(category="trivia")))


def test_answerable_item_needs_url_quote_and_answer():
    errs = errors_for(item(source_url=None, evidence_quote=None, gold_answer=None))
    assert any("'source_url'" in e for e in errs)
    assert any("'evidence_quote'" in e for e in errs)
    assert any("'gold_answer'" in e for e in errs)


def test_unanswerable_needs_searched_for_and_no_quote():
    base = item(category="unanswerable", source_url=None, evidence_quote=None)
    assert any("searched_for" in e for e in errors_for(base))
    ok = dict(base, searched_for="Searched /uas/ for park-specific rules; none found.")
    assert errors_for(ok) == []
    with_quote = dict(ok, source_url=REGISTER, evidence_quote="Registration lasts 3 years.")
    assert any("must not have an evidence_quote" in e for e in errors_for(with_quote))


def test_multi_hop_needs_two_pages():
    one_page = item(category="multi_hop")
    assert any("2+ different pages" in e for e in errors_for(one_page))
    two_pages = item(
        category="multi_hop",
        source_url=[REGISTER, PART107],
        evidence_quote=["Registration lasts 3 years.", "covered by the Part 107 rules"],
    )
    assert errors_for(two_pages) == []


def test_each_url_needs_its_own_quote():
    errs = errors_for(
        item(category="multi_hop", source_url=[REGISTER, PART107], evidence_quote="x")
    )
    assert any("one evidence_quote per source_url" in e for e in errs)


def test_ambiguous_item_without_page_is_allowed():
    amb = item(category="ambiguous", source_url=None, evidence_quote=None)
    assert errors_for(amb) == []


def test_skipping_quote_check_still_checks_length():
    errs, _ = gc.check_item(item(evidence_quote="Not on the page."), pages=None)
    assert errs == []
    errs, _ = gc.check_item(item(evidence_quote=" ".join(["w"] * 30)), pages=None)
    assert any("30 words" in e for e in errs)


# --- Whole-set checks ------------------------------------------------------


def full_set():
    """A set that meets every spec minimum exactly (no targets)."""
    items = []
    for cat, n in gc.MINIMUMS.items():
        for i in range(n):
            it = item(id=f"{cat}-{i}", category=cat, question=f"{cat} question {i}")
            if cat == "multi_hop":
                it["source_url"] = [REGISTER, REMOTE_ID]
                it["evidence_quote"] = [
                    "Registration lasts 3 years.",
                    "share identification and location information",
                ]
            if cat in ("unanswerable", "ambiguous"):
                it.pop("source_url")
                it.pop("evidence_quote")
            if cat == "unanswerable":
                it["searched_for"] = "searched the drone section"
            items.append(it)
    # Pad to the 40-item total with extra single-hop items.
    for i in range(gc.MIN_TOTAL - len(items)):
        items.append(item(id=f"extra-{i}", question=f"extra question {i}"))
    return items


def write_jsonl(path, items):
    path.write_text("\n".join(json.dumps(i) for i in items) + "\n")
    return path


def test_set_meeting_minimums_passes_with_target_warnings(tmp_path):
    golden = write_jsonl(tmp_path / "golden.jsonl", full_set())
    result = gc.run(golden, NORMALIZED)
    assert result["ok"], result["errors"]
    assert result["total_items"] == gc.MIN_TOTAL
    assert any("our target" in w for w in result["warnings"])


def test_below_minimum_and_duplicate_ids_fail(tmp_path):
    items = [i for i in full_set() if i["category"] != "temporal"]
    items.append(dict(items[0]))  # duplicate id
    result = gc.run(write_jsonl(tmp_path / "golden.jsonl", items), NORMALIZED)
    assert not result["ok"]
    assert any("temporal: 0 items" in e for e in result["errors"])
    assert any("used 2 times" in e for e in result["errors"])


def test_bad_json_line_is_reported(tmp_path):
    golden = tmp_path / "golden.jsonl"
    golden.write_text('{"id": "a"}\nnot json\n')
    result = gc.run(golden, NORMALIZED)
    assert any("line 2: not valid JSON" in e for e in result["errors"])


def test_missing_file_fails_clearly(tmp_path):
    result = gc.run(tmp_path / "golden.jsonl", NORMALIZED)
    assert not result["ok"]
    assert "does not exist yet" in result["errors"][0]


@pytest.mark.parametrize("valid", [True, False])
def test_main_writes_artifact_and_sets_exit_code(tmp_path, valid):
    items = full_set() if valid else full_set()[:5]
    golden = write_jsonl(tmp_path / "golden.jsonl", items)
    out = tmp_path / "golden_check.json"
    code = gc.main(["--golden", str(golden), "--normalized", str(NORMALIZED), "--out", str(out)])
    assert code == (0 if valid else 1)
    saved = json.loads(out.read_text())
    assert saved["ok"] is valid
    assert saved["quotas"]["single_hop"]["minimum"] == 15
