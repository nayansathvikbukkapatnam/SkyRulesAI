"""Extra tests for the PII safeguard (D4, ethics safeguard 1).

Covers three layers: the regex filter in src/extract/pii.py, the URL exclusions
in configs/crawl/sources.yaml, and what actually landed in data/normalized/ and
reports/artifacts/pii_log.json. All the contact details below are made up.
"""

import json

import pytest

from src.common import paths
from src.crawl.sources import SourceSet
from src.extract.pii import PATTERNS, redact

# --- The regex filter -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "call 202.555.0100",
        "call 202 555 0100",
        "call +1 202 555 0100",
        "ext. 1234 at 1-800-555-0199",
    ],
)
def test_other_phone_formats_are_redacted(text):
    redacted, counts = redact(text)
    assert counts == {"phone": 1}
    assert "555" not in redacted


def test_email_with_plus_and_subdomain_is_redacted():
    redacted, counts = redact("email A.B+tag@sub.example.org now")
    assert counts == {"email": 1}
    assert "@" not in redacted


def test_every_kind_in_one_block_is_counted_and_removed():
    text = (
        "Questions? Email pat.fixture@example.gov or call (202) 555-0100. "
        "Mail: 800 Independence Avenue SW, Washington."
    )
    redacted, counts = redact(text)
    assert counts == {"email": 1, "phone": 1, "street_address": 1}
    for raw in ("pat.fixture@example.gov", "555-0100", "800 Independence Avenue"):
        assert raw not in redacted


def test_redacting_twice_finds_nothing_new():
    once, _ = redact("call (202) 555-0100 or write jo@example.com")
    twice, counts = redact(once)
    assert twice == once
    assert counts == {}


@pytest.mark.parametrize(
    "text",
    [
        "§ 107.51(b) applies",
        "Category 3 operations",
        "86 FR 4314",
        "effective January 15, 2021",
        "49 U.S.C. 44809",
        "no higher than 1,000 feet",
        "Section 2209",
        "FAA-2018-1087-0001",
        "OMB Control Number 2120-0756",
        "dial 911",
    ],
)
def test_rule_text_numbers_are_not_mistaken_for_pii(text):
    assert redact(text) == (text, {})


@pytest.mark.xfail(strict=True, reason="known gap: P.O. boxes are not redacted (see report 07)")
def test_po_box_is_redacted():
    _, counts = redact("P.O. Box 25082, Oklahoma City")
    assert counts


# --- URL exclusions (pages never fetched) ------------------------------------

SOURCES = SourceSet.load(include_disabled=True)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://knowbeforeyoufly.org/contact-us", "excluded_contact_page"),
        ("https://www.faa.gov/uas/contact_us", "excluded_contact_page"),
        ("https://knowbeforeyoufly.org/wp-login.php", "excluded_login"),
        ("https://knowbeforeyoufly.org/my-account/", "excluded_login"),
        ("https://www.faa.gov/user/login", "excluded_login"),
        ("https://knowbeforeyoufly.org/?s=drone", "excluded_search"),
        ("https://knowbeforeyoufly.org/wp-admin/", "excluded_site_admin"),
        (
            "https://www.faa.gov/uas/commercial_operators/part_107_waivers/waivers_issued",
            "excluded_waivers_issued",
        ),
        (
            "https://www.faa.gov/uas/resources/public_records/uas_sightings_report",
            "excluded_sightings",
        ),
        ("https://faadronezone-access.faa.gov/", "other_host"),
    ],
)
def test_contact_login_and_people_lists_are_never_fetched(url, reason):
    decision, got_reason, _ = SOURCES.scope(url)
    assert decision == "skip"
    assert got_reason == reason


# --- What actually landed on disk ---------------------------------------------

NORMALIZED = sorted(paths.NORMALIZED.glob("*.json"))


@pytest.mark.skipif(not NORMALIZED, reason="data/normalized/ is empty")
def test_committed_corpus_has_no_contact_details_left():
    leftovers = []
    for p in NORMALIZED:
        doc = json.loads(p.read_text(encoding="utf-8"))
        parts = [doc["title"]]
        for sec in doc["sections"]:
            parts += [sec["text"], *sec["tables"]]
        text = "\n".join(parts)
        leftovers += [(doc["doc_id"], kind) for kind, rx in PATTERNS.items() if rx.search(text)]
    assert leftovers == []


PII_LOG = paths.ARTIFACTS / "pii_log.json"


@pytest.mark.skipif(not PII_LOG.exists(), reason="no pii_log.json yet")
def test_pii_log_holds_counts_only():
    log = json.loads(PII_LOG.read_text(encoding="utf-8"))
    assert set(log["totals"]) <= set(PATTERNS)
    assert all(isinstance(n, int) for n in log["totals"].values())
    for counts in log["by_doc"].values():
        assert set(counts) <= set(PATTERNS)
        assert all(isinstance(n, int) for n in counts.values())
    assert "@" not in PII_LOG.read_text(encoding="utf-8")
