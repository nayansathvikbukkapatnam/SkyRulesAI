"""Fill the tables in reports/01_ingestion.md from reports/artifacts/ (CLAUDE.md rule 6).

Same convention as src/reports/chunking.py: prose is written by hand, every number
sits between <!-- BEGIN AUTO:<block> --> and <!-- END AUTO:<block> --> markers and
is rewritten here from the artifacts. A missing artifact renders as "not produced
yet" with the command that produces it.

    make crawl-summary        # reports/artifacts/crawl_summary.json (all crawl runs)
    make report-ingestion     # fills the tables below
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from src.common import paths
from src.crawl.sources import SourceSet
from src.reports.chunking import _load, _missing, _table

REPORT = paths.REPORTS / "01_ingestion.md"


def block_crawl_settings(artifacts: Path) -> str:
    stats = _load(artifacts, "crawl_stats.json")
    if not stats:
        return _missing("crawl stats", "make crawl")
    limits = stats["limits"]
    enabled = {src.name for src in SourceSet.load().sources}
    rows = [
        ["User-Agent", f"`{stats['user_agent']}` (address from `.env`)"],
        ["Minimum gap between requests (s)", limits["min_interval_s"]],
        ["Attempts per URL on 429 / 5xx (exponential backoff)", limits["max_attempts"]],
        ["Link depth from a seed", limits["max_depth"]],
        ["URL cap, all sources", limits["max_urls_total"]],
        *[
            [
                f"URL cap, `{src.name}` ({src.host})"
                + ("" if src.name in enabled else ", disabled"),
                src.max_urls,
            ]
            for src in SourceSet.load(include_disabled=True).sources
        ],
    ]
    return _table(["Setting", "Value"], rows) + (
        "\n\nSources: `reports/artifacts/crawl_stats.json` (latest run) and the per-source "
        "caps in `configs/crawl/sources.yaml`."
    )


def block_latest_run(artifacts: Path) -> str:
    stats = _load(artifacts, "crawl_stats.json")
    if not stats:
        return _missing("crawl stats", "make crawl")
    rows = [
        ["Started / finished (UTC)", f"{stats['started_at']} / {stats['finished_at']}"],
        ["HTTP requests", stats["requests_this_run"]],
        ["Smallest measured gap between requests (s)", stats["min_gap_between_requests_s"]],
        ["robots.txt checks", stats["robots"]["checks"]],
        ["URLs refused by robots.txt", stats["robots"]["disallowed"]],
        ["Documents saved", stats["saved_this_run"]],
        ["Already in the manifest, reused (not re-fetched)", stats["resumed_from_manifest"]],
    ]
    return (
        _table(["Latest run", "Value"], rows) + "\n\nSource: `reports/artifacts/crawl_stats.json`."
    )


def block_raw_store(artifacts: Path) -> str:
    s = _load(artifacts, "crawl_summary.json")
    if not s:
        return _missing("crawl summary", "make crawl-summary")
    types = sorted(s["by_content_type"])
    rows = [
        [f"`{name}`", *[c.get(t, 0) for t in types], sum(c.values())]
        for name, c in s["by_source"].items()
    ]
    rows.append(["**Total**", *[s["by_content_type"][t] for t in types], s["manifest_entries"]])
    disabled = ", ".join(f"`{k}`: {v}" for k, v in s["entries_from_disabled_sources"].items())
    return (
        _table(["Source", *types, "Saved fetches"], rows)
        + f"\n\nDistinct SHA-256 among saved files: {s['unique_sha256']}. "
        f"First and last fetch (UTC): {s['first_fetch']} and {s['last_fetch']}. "
        f"Files from sources now disabled: {disabled or 'none'}; quarantined raw files: "
        f"{s['quarantined_files']}. Source: `reports/artifacts/crawl_summary.json`."
    )


def block_skipped(artifacts: Path) -> str:
    s = _load(artifacts, "crawl_summary.json")
    if not s:
        return _missing("crawl summary", "make crawl-summary")
    rows = [[f"`{reason}`", n] for reason, n in s["skipped_unique_by_reason"].items()]
    rows.append(["**Total distinct URLs**", s["skipped_unique_urls"]])
    robots = ", ".join(f"`{k}`: {v}" for k, v in s["robots_disallowed_by_source"].items())
    return (
        _table(["Reason (never requested, or not saved)", "Distinct URLs"], rows)
        + f"\n\nURLs refused by robots.txt, by source: {robots or 'none'}. A URL skipped in "
        f"several runs is counted once ({s['skipped_records']} log lines in all). Source: "
        "`reports/artifacts/crawl_summary.json`."
    )


def block_corpus(artifacts: Path) -> str:
    s = _load(artifacts, "ingestion_stats.json")
    if not s:
        return _missing("ingestion stats", "make extract")
    rows = [
        ["Normalized documents written", s["documents_written"]],
        *[[f"&nbsp;&nbsp;by source: `{k}`", v] for k, v in s["by_source"].items()],
        *[[f"&nbsp;&nbsp;by type: `{k}`", v] for k, v in s["by_content_type"].items()],
        *[[f"&nbsp;&nbsp;by page_type: `{k}`", v] for k, v in s["by_page_type"].items()],
        *[[f"&nbsp;&nbsp;by audience: `{k}`", v] for k, v in s["by_audience"].items()],
        ["Sections", s["sections"]],
        ["Tables kept as Markdown", s["tables"]],
        ["Documents with at least one table", s["documents_with_tables"]],
        ["Duplicates removed (same bytes or same extracted content)", s["duplicates_removed"]],
        ["Extraction failures", len(s["failures"])],
        ["Reserved CFR sections skipped (no text)", len(s["reserved_sections_skipped"])],
        *[
            [f"Excluded by `sources.yaml`: {k}", v]
            for k, v in s["excluded_by_source_config"].items()
        ],
    ]
    return (
        _table(["Corpus", "Count"], rows) + "\n\nSource: `reports/artifacts/ingestion_stats.json`."
    )


def block_methods(artifacts: Path) -> str:
    s = _load(artifacts, "ingestion_stats.json")
    if not s:
        return _missing("ingestion stats", "make extract")
    rows = [[f"`{k}`", v] for k, v in s["extraction_method"].items()]
    return _table(["Extraction method", "Documents"], rows) + (
        f"\n\nImage-only PDF pages (no text layer, OCR not run): {s['pages_needing_ocr']} pages "
        f"in {s['documents_needing_ocr']} documents. "
        "Source: `reports/artifacts/ingestion_stats.json`."
    )


def block_dates(artifacts: Path) -> str:
    s = _load(artifacts, "ingestion_stats.json")
    if not s:
        return _missing("ingestion stats", "make extract")
    found = s["effective_date_found_by_source"]
    rows = [[f"`{k}`", n, found.get(k, 0)] for k, n in s["by_source"].items()]
    rows.append(["**Total**", s["documents_written"], s["effective_date_found"]])
    return (
        _table(["Source", "Documents", "effective_date found"], rows)
        + "\n\nSource: `reports/artifacts/ingestion_stats.json`."
    )


def block_pii(artifacts: Path) -> str:
    p = _load(artifacts, "pii_log.json")
    if not p:
        return _missing("PII log", "make extract")
    rows = [[f"`{k}`", v] for k, v in p["totals"].items()]
    rows.append(["**Documents with at least one redaction**", p["documents_with_redactions"]])
    return (
        _table(["Redacted item", "Count"], rows)
        + "\n\nCounts only; the matched values are never logged. Source: "
        "`reports/artifacts/pii_log.json`."
    )


BLOCKS: dict[str, Callable[[Path], str]] = {
    "crawl_settings": block_crawl_settings,
    "latest_run": block_latest_run,
    "raw_store": block_raw_store,
    "skipped": block_skipped,
    "corpus": block_corpus,
    "methods": block_methods,
    "dates": block_dates,
    "pii": block_pii,
}

_BLOCK = re.compile(r"(<!-- BEGIN AUTO:(\w+) -->)\n(.*?)\n?(<!-- END AUTO:\2 -->)", re.S)


def render(text: str, artifacts: Path) -> str:
    """Replace every AUTO block's body. Unknown or unclosed blocks are an error."""
    seen: list[str] = []

    def fill(m: re.Match) -> str:
        name = m.group(2)
        if name not in BLOCKS:
            raise ValueError(f"unknown AUTO block {name!r}; known: {sorted(BLOCKS)}")
        seen.append(name)
        return f"{m.group(1)}\n{BLOCKS[name](artifacts)}\n{m.group(4)}"

    out = _BLOCK.sub(fill, text)
    opened = re.findall(r"<!-- BEGIN AUTO:(\w+) -->", text)
    if sorted(opened) != sorted(seen):
        raise ValueError(f"unclosed AUTO block(s): {sorted(set(opened) - set(seen))}")
    return out


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fill report 01's tables from artifacts.")
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--artifacts", type=Path, default=paths.ARTIFACTS)
    args = parser.parse_args(list(argv) if argv is not None else None)
    out = render(args.report.read_text(encoding="utf-8"), args.artifacts)
    args.report.write_text(out, encoding="utf-8")
    print(f"updated {args.report} ({out.count('_Not produced yet')} block(s) waiting)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
