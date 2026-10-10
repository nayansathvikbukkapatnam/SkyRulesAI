"""Summarize every crawl run from the crawl logs into reports/artifacts/crawl_summary.json.

crawl_stats.json describes only the latest run, because each run rewrites it. The
logs keep everything: data/raw/manifest.jsonl (one line per saved fetch) and
data/raw/skipped.jsonl (one line per URL skipped in a run, with its reason). This
reads both so report 01 can give totals across all runs.

    make crawl-summary
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from src.common import paths
from src.crawl.crawler import read_manifest
from src.crawl.sources import SourceSet


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def summarize(
    manifest_path: Path = paths.MANIFEST,
    skipped_path: Path = paths.SKIPPED,
    quarantine_dir: Path = paths.RAW / "quarantine",
) -> dict:
    entries = read_manifest(manifest_path)
    skipped = _read_jsonl(skipped_path)
    sources = SourceSet.load(include_disabled=True)
    enabled = SourceSet.load()

    def source(url: str) -> str:
        found = sources.source_for(url)
        return found.name if found else (urlsplit(url).hostname or "?")

    by_source: dict[str, Counter] = {}
    for e in entries:
        by_source.setdefault(source(e["url"]), Counter())[e["content_type"]] += 1

    # A URL can be skipped in several runs; count it once, under its latest reason.
    latest = {r["url"]: r for r in skipped}
    by_reason = Counter(r["reason"] for r in latest.values())
    robots = Counter(
        source(r["url"]) for r in latest.values() if r["reason"].endswith("robots_disallowed")
    )
    fetched = sorted(e["fetched_at"] for e in entries)
    return {
        "command": "make crawl-summary",
        "manifest_entries": len(entries),
        "unique_sha256": len({e["sha256"] for e in entries}),
        "first_fetch": fetched[0] if fetched else None,
        "last_fetch": fetched[-1] if fetched else None,
        "by_source": {k: dict(v) for k, v in sorted(by_source.items())},
        "by_content_type": dict(Counter(e["content_type"] for e in entries)),
        # What the checker decided at fetch time. For eCFR that decision was wrong (a
        # parser bug hid its Disallow rule); those files belong to a disabled source.
        "robots_allowed_as_recorded_at_fetch": sum(e["robots_allowed"] for e in entries),
        "entries_from_disabled_sources": {
            name: n
            for name, n in Counter(
                source(e["url"]) for e in entries if not enabled.source_for(e["url"])
            ).items()
        },
        "skipped_records": len(skipped),
        "skipped_unique_urls": len(latest),
        "skipped_unique_by_reason": dict(sorted(by_reason.items())),
        "robots_disallowed_by_source": dict(sorted(robots.items())),
        "quarantined_files": len(list(quarantine_dir.glob("*.bin"))),
    }


def main() -> int:
    if not paths.MANIFEST.exists():
        print(f"no manifest at {paths.MANIFEST}; run `make crawl` first")
        return 1
    summary = summarize()
    paths.ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = paths.ARTIFACTS / "crawl_summary.json"
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
