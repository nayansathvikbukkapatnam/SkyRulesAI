"""data/raw/ to data/normalized/{doc_id}.json, plus ingestion stats (task A2).

For each manifest entry: pick the extractor for its source and content type,
fill page_type, audience, effective_date and a citable source_url by rule,
redact personal contact details, drop exact duplicates by content hash, and
write one NormalizedDoc. Run with `make extract` (or `python -m src.extract.normalize`).

Writes reports/artifacts/ingestion_stats.json and reports/artifacts/pii_log.json.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from src.common import paths
from src.common.contracts import (
    NormalizedDoc,
    doc_id_for,
    sha256_hex,
    validate_normalized_doc,
)
from src.crawl.crawler import read_manifest
from src.crawl.sources import SKIP, SOURCES_FILE, SourceSet
from src.extract import html as html_x
from src.extract.cfr import cfr_section
from src.extract.pdf import extract_pdf
from src.extract.pii import redact

log = logging.getLogger(__name__)

HTML, PDF, XML = "text/html", "application/pdf", "application/xml"
FR_NUMBER = re.compile(r"(\d{4}-\d{5})\.(?:html|pdf)$")
# govinfo annual CFR edition, one XML per section, e.g. CFR-2025-title14-vol2-sec107-31.xml
CFR_SECTION = re.compile(r"CFR-(\d{4})-title14-vol\d-sec(\d+)-(\d+)\.xml$")
MONTH_DATE = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}"
)
EFFECTIVE = re.compile(rf"\beffective\s+(?:on\s+)?({MONTH_DATE})")
# In the PDF layout the contact block is inline text, not a heading: drop it up to
# the next labelled block (rule 2; the HTML path drops it as a section).
FR_CONTACT = re.compile(
    r"FOR FURTHER INFORMATION CONTACT\s*:.*?(?=SUPPLEMENTARY INFORMATION\s*:|$)", re.S
)


def fr_documents(path: Path = SOURCES_FILE) -> dict[str, dict]:
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    for source in raw["sources"]:
        if source["name"] == "federalregister":
            return {str(k): v for k, v in (source.get("documents") or {}).items()}
    return {}


def _iso(month_date: str) -> str:
    return datetime.strptime(month_date, "%B %d, %Y").date().isoformat()


def _effective_date(sections, published: str | None) -> str | None:
    """The rule's stated effective date (from its DATES section), else its publication date."""
    dates = [s for s in sections if any("DATES" in h for h in s["heading_path"])]
    for section in dates or sections[:5]:
        m = EFFECTIVE.search(section["text"])
        if m:
            return _iso(m.group(1))
    return published


def _keyword_audience(url: str) -> str:
    u = url.lower()
    if "recreational" in u:
        return "recreational"
    if "commercial" in u:
        return "part107"
    if re.search(r"government|public[-_]?safety", u):
        return "public_safety"
    return "general"


def extract_entry(
    entry: dict, raw: bytes, fr_docs: dict[str, dict]
) -> tuple[dict, html_x.Extracted]:
    """Metadata and extracted sections for one manifest entry."""
    url, kind = entry["final_url"], entry["content_type"]
    host = urlsplit(url).hostname or ""
    number = m.group(1) if (m := FR_NUMBER.search(url)) else None
    fr = fr_docs.get(number or "", {})

    if host == "www.ecfr.gov":
        ext, public_path = html_x.ecfr_section(raw)
        part = re.search(r"section-(\d+)\.", public_path or "")
        meta = {
            "source_url": f"https://www.ecfr.gov{public_path}" if public_path else url,
            "page_type": "regulation_summary",
            "audience": "part107" if part and part.group(1) == "107" else "general",
            # The renderer API gives no per-section date; the text is current as of the crawl.
            "effective_date": None,
        }
        if not public_path:
            ext.warnings.append("no public eCFR path found; source_url is the API URL")
        return meta, ext

    if host == "www.govinfo.gov" and kind == XML and CFR_SECTION.search(url):
        ext, number, edition = cfr_section(raw)
        meta = {
            "source_url": url,
            "page_type": "regulation_summary",
            "audience": "part107" if (number or "").startswith("107.") else "general",
            # The annual edition is the text in force on its date (January 1).
            "effective_date": edition,
        }
        return meta, ext

    if host in ("www.federalregister.gov", "www.govinfo.gov") and fr:
        label = f"{fr['title']} (Federal Register {fr['type']}, {fr['published']})"
        if kind == PDF:
            ext = extract_pdf(raw, title_hint=f"{label} [PDF]")
            for section in ext.sections:
                section["text"], n = FR_CONTACT.subn("", section["text"])
                if n:
                    ext.warnings.append("dropped the FOR FURTHER INFORMATION CONTACT block")
            ext.sections = [s for s in ext.sections if s["text"].strip() or s["tables"]]
            source_url = url
        else:
            ext = html_x.federal_register(raw, label)
            source_url = f"https://www.federalregister.gov/d/{number}"
        meta = {
            "source_url": source_url,
            "page_type": "regulation_summary",
            "audience": fr.get("audience", "general"),
            "effective_date": _effective_date(ext.sections, fr.get("published")),
        }
        return meta, ext

    # Ordinary pages and PDFs (Know Before You Fly, faa.gov if it ever opens up).
    ext = extract_pdf(raw) if kind == PDF else html_x.web_page(raw)
    meta = {
        "source_url": url,
        "page_type": "toolkit_pdf" if kind == PDF else "guide_page",
        "audience": _keyword_audience(url),
        "effective_date": ext.date,
    }
    return meta, ext


def _redact_doc(doc: NormalizedDoc) -> Counter:
    total: Counter = Counter()
    doc["title"], c = redact(doc["title"])
    total += c
    for section in doc["sections"]:
        section["text"], c = redact(section["text"])
        total += c
        new_tables = []
        for table in section["tables"]:
            table, c = redact(table)
            total += c
            new_tables.append(table)
        section["tables"] = new_tables
        new_path = []
        for heading in section["heading_path"]:
            heading, c = redact(heading)
            total += c
            new_path.append(heading)
        section["heading_path"] = new_path
    return total


def content_hash(sections) -> str:
    parts = []
    for s in sections:
        parts += [" > ".join(s["heading_path"]), s["text"], *s["tables"]]
    return sha256_hex("\n".join(parts))


def _raw_path(entry: dict) -> Path:
    path = Path(entry["path"])
    return path if path.is_absolute() else paths.ROOT / path


def normalize(
    manifest_path: Path = paths.MANIFEST,
    out_dir: Path = paths.NORMALIZED,
    artifacts: Path = paths.ARTIFACTS,
) -> dict:
    entries = read_manifest(manifest_path)
    fr_docs = fr_documents()
    # Only files that sources.yaml still covers: a disabled source's files (eCFR,
    # fetched against its robots.txt) and replaced files are never in the corpus.
    enabled = SourceSet.load()
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)

    stats: Counter = Counter()
    by: dict[str, Counter] = {
        k: Counter() for k in ("content_type", "source", "page_type", "audience", "method")
    }
    seen_raw: set[str] = set()
    seen_content: dict[str, str] = {}
    failures, duplicates, reserved, ocr_needed = [], [], [], {}
    pii_total: Counter = Counter()
    pii_by_doc: dict[str, dict] = {}
    dates_found: Counter = Counter()
    excluded_sources: Counter = Counter()
    written: set[str] = set()

    for entry in entries:
        host = urlsplit(entry["final_url"]).hostname or "?"
        if enabled.source_for(entry["final_url"]) is None:
            excluded_sources[f"{host} (source disabled)"] += 1
            continue
        if enabled.scope(entry["final_url"])[0] == SKIP:
            # e.g. the govinfo CFR section PDFs, replaced by the section XML
            excluded_sources[f"{host} (no longer in scope)"] += 1
            continue
        if entry["sha256"] in seen_raw:
            duplicates.append({"url": entry["url"], "reason": "same raw bytes"})
            continue
        seen_raw.add(entry["sha256"])
        source = (urlsplit(entry["final_url"]).hostname or "").removeprefix("www.")
        try:
            meta, ext = extract_entry(entry, _raw_path(entry).read_bytes(), fr_docs)
        except Exception as e:  # one bad file must not stop the run
            failures.append({"url": entry["url"], "error": f"{type(e).__name__}: {e}"})
            continue
        if not ext.sections:
            if "reserved section (no text)" in ext.warnings:
                reserved.append(entry["url"])
            else:
                failures.append(
                    {"url": entry["url"], "error": "no text extracted", "warnings": ext.warnings}
                )
            continue

        doc_id = doc_id_for(meta["source_url"])
        doc = NormalizedDoc(
            doc_id=doc_id,
            source_url=meta["source_url"],
            title=ext.title or meta["source_url"],
            page_type=meta["page_type"],
            audience=meta["audience"],
            effective_date=meta["effective_date"],
            crawl_ts=entry["fetched_at"],
            content_hash="",
            sections=ext.sections,
            extraction={"method": ext.method, "ocr_used": False, "warnings": ext.warnings},
        )
        pii = _redact_doc(doc)
        doc["content_hash"] = content_hash(doc["sections"])
        if doc["content_hash"] in seen_content:
            duplicates.append(
                {
                    "url": entry["url"],
                    "reason": f"same content as {seen_content[doc['content_hash']]}",
                }
            )
            continue
        seen_content[doc["content_hash"]] = doc_id
        errors = validate_normalized_doc(doc)
        if errors:
            failures.append({"url": entry["url"], "error": "; ".join(errors)})
            continue

        (out_dir / f"{doc_id}.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        written.add(doc_id)
        stats["documents"] += 1
        stats["sections"] += len(doc["sections"])
        n_tables = sum(len(s["tables"]) for s in doc["sections"])
        stats["tables"] += n_tables
        stats["documents_with_tables"] += bool(n_tables)
        by["content_type"][entry["content_type"]] += 1
        by["source"][source] += 1
        by["page_type"][doc["page_type"]] += 1
        by["audience"][doc["audience"]] += 1
        by["method"][ext.method] += 1
        if doc["effective_date"]:
            dates_found[source] += 1
        ocr_pages = [w for w in ext.warnings if "needs OCR" in w]
        if ocr_pages:
            ocr_needed[doc_id] = len(ocr_pages)
        if pii:
            pii_total += pii
            pii_by_doc[doc_id] = dict(pii)
        log.info(
            "%s %-18s %3d sections %s", doc_id, ext.method, len(doc["sections"]), doc["title"][:70]
        )

    stale = [p for p in out_dir.glob("*.json") if p.stem not in written]
    for p in stale:
        p.unlink()  # from an earlier run; data/normalized always mirrors the manifest

    stats_out = {
        "command": "make extract",
        "manifest_entries": len(entries),
        "excluded_by_source_config": dict(excluded_sources),
        "documents_written": stats["documents"],
        "by_content_type": dict(by["content_type"]),
        "by_source": dict(by["source"]),
        "by_page_type": dict(by["page_type"]),
        "by_audience": dict(by["audience"]),
        "extraction_method": dict(by["method"]),
        "sections": stats["sections"],
        "tables": stats["tables"],
        "documents_with_tables": stats["documents_with_tables"],
        "duplicates_removed": len(duplicates),
        "duplicates": duplicates,
        "failures": failures,
        "reserved_sections_skipped": reserved,
        "effective_date_found": sum(dates_found.values()),
        "effective_date_found_by_source": dict(dates_found),
        "documents_needing_ocr": len(ocr_needed),
        "pages_needing_ocr": sum(ocr_needed.values()),
        "pii_redactions": dict(pii_total),
        "stale_files_removed": len(stale),
    }
    (artifacts / "ingestion_stats.json").write_text(json.dumps(stats_out, indent=2) + "\n")
    pii_log = {
        "command": "make extract",
        "note": "counts only; matched values are never logged",
        "totals": dict(pii_total),
        "documents_with_redactions": len(pii_by_doc),
        "by_doc": pii_by_doc,
    }
    (artifacts / "pii_log.json").write_text(json.dumps(pii_log, indent=2) + "\n")
    return stats_out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not paths.MANIFEST.exists():
        print(f"no manifest at {paths.MANIFEST}; run `make crawl` first")
        return 1
    stats = normalize()
    summary = {k: v for k, v in stats.items() if k not in ("duplicates", "failures")}
    print(json.dumps(summary, indent=2))
    print(f"failures: {len(stats['failures'])}  duplicates: {stats['duplicates_removed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
