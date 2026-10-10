"""Report 01 tables come only from artifacts, and the crawl summary counts all runs."""

import json

import pytest

from src.crawl.summary import summarize
from src.reports.ingestion import render

ARTIFACTS = {
    "crawl_stats.json": {
        "user_agent": "SkyRulesAI-MSAI633 (+<CRAWL_CONTACT_EMAIL>)",
        "limits": {"min_interval_s": 1.0, "max_attempts": 4, "max_depth": 3, "max_urls_total": 600},
        "started_at": "s",
        "finished_at": "f",
        "requests_this_run": 36,
        "min_gap_between_requests_s": 1.0,
        "robots": {"checks": 35, "disallowed": 0},
        "saved_this_run": 32,
        "resumed_from_manifest": 88,
    },
    "pii_log.json": {"totals": {"email": 2}, "documents_with_redactions": 1},
}


def test_render_fills_blocks_from_artifacts_and_marks_missing_ones(tmp_path):
    for name, data in ARTIFACTS.items():
        (tmp_path / name).write_text(json.dumps(data))
    text = (
        "intro\n<!-- BEGIN AUTO:latest_run -->\nold\n<!-- END AUTO:latest_run -->\n"
        "<!-- BEGIN AUTO:pii -->\n<!-- END AUTO:pii -->\n"
        "<!-- BEGIN AUTO:corpus -->\n<!-- END AUTO:corpus -->\n"
    )
    out = render(text, tmp_path)
    assert "| HTTP requests | 36 |" in out and "old" not in out
    assert "| `email` | 2 |" in out
    assert "_Not produced yet: ingestion stats. Run `make extract`._" in out
    assert out.startswith("intro\n")


def test_render_rejects_unknown_blocks(tmp_path):
    with pytest.raises(ValueError):
        render("<!-- BEGIN AUTO:nope -->\n<!-- END AUTO:nope -->", tmp_path)


def test_summary_counts_every_run_and_each_skipped_url_once(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    entries = [
        ("https://knowbeforeyoufly.org/", "text/html", "a"),
        ("https://www.ecfr.gov/api/renderer/v1/content/enhanced/current/title-14?section=107.1",
         "text/html", "b"),
    ]  # fmt: skip
    manifest.write_text(
        "".join(
            json.dumps(
                {"url": u, "final_url": u, "fetched_at": f"2026-10-10T19:0{i}:00+00:00",
                 "content_type": t, "sha256": h, "robots_allowed": True}
            ) + "\n"
            for i, (u, t, h) in enumerate(entries)
        )
    )  # fmt: skip
    skipped = tmp_path / "skipped.jsonl"
    skipped.write_text(
        "".join(
            json.dumps({"url": u, "reason": r}) + "\n"
            for u, r in [
                ("https://www.faa.gov/uas", "robots_disallowed"),
                ("https://www.faa.gov/uas", "robots_disallowed"),  # same URL, a later run
                ("https://x.com/a", "other_host"),
            ]
        )
    )
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    (quarantine / "b.bin").write_bytes(b"x")

    s = summarize(manifest, skipped, quarantine)
    assert s["manifest_entries"] == 2
    assert s["by_source"] == {"ecfr": {"text/html": 1}, "knowbeforeyoufly": {"text/html": 1}}
    assert s["entries_from_disabled_sources"] == {"ecfr": 1}
    assert s["skipped_records"] == 3 and s["skipped_unique_urls"] == 2
    assert s["skipped_unique_by_reason"] == {"other_host": 1, "robots_disallowed": 1}
    assert s["robots_disallowed_by_source"] == {"faa": 1}
    assert s["quarantined_files"] == 1
