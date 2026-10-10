"""Validate eval/golden.jsonl before anything is evaluated against it (make golden-check).

Checks every item against the contract in CLAUDE.md section 5 and the rules in
docs/DELIVERABLES.md section 3:

- schema: required fields per category, known category, unique ids
- evidence quotes: 25 words or fewer, and found in the saved normalized text of
  the page they cite (data/normalized/{doc_id}.json), so the quote is real
- human verification: verified_by is filled in and is not the author
- quotas: spec minimums (errors) and our targets (warnings), 40+ items in total

This script only checks items. The items themselves are written and verified by
team members (CLAUDE.md rule 4).

Writes reports/artifacts/golden_check.json for reports/03_golden_set.md and
exits non-zero if any error is found.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path

from src.common import paths
from src.common.contracts import CATEGORIES, doc_id_for

MAX_QUOTE_WORDS = 25
MIN_TOTAL = 40

# Spec minimums (docs/DELIVERABLES.md section 3); below these is an error.
MINIMUMS = {
    "single_hop": 15,
    "multi_hop": 6,
    "comparative": 4,
    "temporal": 4,
    "unanswerable": 8,
    "ambiguous": 3,
}
# Our targets (reports/00_proposal.md section 9.1); below these is a warning.
TARGETS = {
    "single_hop": 18,
    "multi_hop": 7,
    "comparative": 5,
    "temporal": 5,
    "unanswerable": 10,
    "ambiguous": 5,
}

# Categories that must point at a page and quote it. Ambiguous items may cite a
# page (checked if they do) but adversarial ones often have none.
ANSWERABLE = {"single_hop", "multi_hop", "comparative", "temporal"}
COMMON_FIELDS = ["id", "category", "question", "author", "verified_by", "verified_date"]


# --- Text normalization -----------------------------------------------------

_PUNCT_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        " ": " ",
    }
)


def normalize_text(text: str) -> str:
    """Make quote matching robust to case, curly quotes, dashes and whitespace only.

    Words and punctuation must still match, so a paraphrase is not accepted.
    """
    text = unicodedata.normalize("NFKC", text).translate(_PUNCT_MAP)
    return re.sub(r"\s+", " ", text).strip().casefold()


def word_count(text: str) -> int:
    return len(text.split())


def page_text(doc: dict) -> str:
    """All searchable text of a normalized doc: title, headings, section text, tables."""
    parts = [doc.get("title", "")]
    for sec in doc.get("sections", []):
        parts.extend(sec.get("heading_path", []))
        parts.append(sec.get("text", ""))
        for table in sec.get("tables", []):
            parts.append(table)
            # Also let a quote match a table row without the Markdown pipes.
            parts.append(re.sub(r"\s*\|\s*", " ", table))
    return normalize_text("\n".join(parts))


# --- Loading ----------------------------------------------------------------


def load_items(path: Path) -> tuple[list[dict], list[str]]:
    items, errors = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"line {n}: not valid JSON ({e.msg})")
            continue
        if not isinstance(item, dict):
            errors.append(f"line {n}: expected a JSON object")
            continue
        item["_line"] = n
        items.append(item)
    return items, errors


class PageStore:
    """Looks up the saved normalized text of a cited URL, caching each page."""

    def __init__(self, normalized_dir: Path):
        self.dir = normalized_dir
        self._cache: dict[str, str | None] = {}

    def text_for(self, url: str) -> str | None:
        doc_id = doc_id_for(url)
        if doc_id not in self._cache:
            p = self.dir / f"{doc_id}.json"
            self._cache[doc_id] = (
                page_text(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else None
            )
        return self._cache[doc_id]


# --- Per-item checks --------------------------------------------------------


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def check_item(item: dict, pages: PageStore | None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one item. pages=None skips the quote-on-page check."""
    where = f"line {item.get('_line', '?')} ({item.get('id', 'no id')})"
    errors: list[str] = []
    warnings: list[str] = []

    for key in COMMON_FIELDS:
        if _blank(item.get(key)):
            errors.append(f"{where}: missing '{key}'")

    category = item.get("category")
    if category is not None and category not in CATEGORIES:
        errors.append(
            f"{where}: unknown category {category!r}; expected one of {sorted(CATEGORIES)}"
        )
        return errors, warnings

    # Human verification: someone other than the author checked it against the page.
    author = str(item.get("author") or "").strip().casefold()
    verifier = str(item.get("verified_by") or "").strip().casefold()
    if author and verifier and author == verifier:
        errors.append(f"{where}: verified_by must be a different person from author")
    if not _blank(item.get("verified_date")):
        try:
            date.fromisoformat(str(item["verified_date"]))
        except ValueError:
            errors.append(f"{where}: verified_date must be YYYY-MM-DD")

    urls = _as_list(item.get("source_url"))
    quotes = _as_list(item.get("evidence_quote"))

    if category == "unanswerable":
        if _blank(item.get("searched_for")):
            errors.append(f"{where}: unanswerable items must record 'searched_for'")
        if quotes:
            errors.append(f"{where}: unanswerable items must not have an evidence_quote")
        return errors, warnings

    if category in ANSWERABLE or category == "ambiguous":
        if _blank(item.get("gold_answer")):
            errors.append(f"{where}: missing 'gold_answer'")

    if category in ANSWERABLE:
        if not urls:
            errors.append(f"{where}: missing 'source_url'")
        if not quotes:
            errors.append(f"{where}: missing 'evidence_quote'")
    if category == "multi_hop" and len({u for u in urls if isinstance(u, str)}) < 2:
        errors.append(f"{where}: multi_hop items need source_url from 2+ different pages")

    if urls and quotes and len(quotes) != len(urls):
        errors.append(
            f"{where}: give one evidence_quote per source_url "
            f"({len(urls)} urls, {len(quotes)} quotes)"
        )
        return errors, warnings

    for url, quote in zip(urls, quotes, strict=False):
        if not isinstance(url, str) or not isinstance(quote, str) or not quote.strip():
            errors.append(f"{where}: source_url and evidence_quote must be non-empty strings")
            continue
        n_words = word_count(quote)
        if n_words > MAX_QUOTE_WORDS:
            errors.append(f"{where}: evidence_quote has {n_words} words (max {MAX_QUOTE_WORDS})")
        if pages is None:
            continue
        try:
            text = pages.text_for(url)
        except ValueError:
            errors.append(f"{where}: source_url is not an absolute URL: {url!r}")
            continue
        if text is None:
            errors.append(f"{where}: {url} is not in the saved corpus (data/normalized/)")
        elif normalize_text(quote) not in text:
            errors.append(f"{where}: evidence_quote not found verbatim on {url}")

    return errors, warnings


# --- Whole-set checks -------------------------------------------------------


def check_set(items: list[dict]) -> tuple[list[str], list[str], dict]:
    errors: list[str] = []
    warnings: list[str] = []

    ids = Counter(item.get("id") for item in items if item.get("id"))
    for dup, n in ids.items():
        if n > 1:
            errors.append(f"id {dup!r} is used {n} times")

    questions = Counter(normalize_text(str(item.get("question", ""))) for item in items)
    for q, n in questions.items():
        if q and n > 1:
            warnings.append(f"the same question appears {n} times: {q[:60]!r}")

    counts = Counter(item.get("category") for item in items if item.get("category") in CATEGORIES)
    quotas = {}
    for cat in MINIMUMS:
        have = counts.get(cat, 0)
        quotas[cat] = {"count": have, "minimum": MINIMUMS[cat], "target": TARGETS[cat]}
        if have < MINIMUMS[cat]:
            errors.append(f"{cat}: {have} items, spec minimum is {MINIMUMS[cat]}")
        elif have < TARGETS[cat]:
            warnings.append(f"{cat}: {have} items, our target is {TARGETS[cat]}")
    if len(items) < MIN_TOTAL:
        errors.append(f"{len(items)} items in total, spec minimum is {MIN_TOTAL}")

    return errors, warnings, quotas


def _display(path: Path) -> str:
    """Repo-relative path, so the committed artifact holds no local folder names."""
    try:
        return str(path.resolve().relative_to(paths.ROOT))
    except ValueError:
        return str(path)


def run(golden: Path, normalized_dir: Path, check_quotes: bool = True) -> dict:
    """Check the whole file and return the result that is saved as the artifact."""
    if not golden.exists():
        return {
            "golden_file": _display(golden),
            "ok": False,
            "errors": [f"{golden} does not exist yet"],
            "warnings": [],
        }

    items, errors = load_items(golden)
    warnings: list[str] = []
    pages = PageStore(normalized_dir) if check_quotes else None
    if not check_quotes:
        warnings.append("evidence quotes were NOT checked against the saved pages (--no-quotes)")

    bad_items = set()
    for item in items:
        item_errors, item_warnings = check_item(item, pages)
        if item_errors:
            bad_items.add(item.get("id"))
        errors += item_errors
        warnings += item_warnings

    set_errors, set_warnings, quotas = check_set(items)
    errors += set_errors
    warnings += set_warnings

    authors = Counter(str(i["author"]).strip() for i in items if i.get("author"))
    verifiers = Counter(str(i["verified_by"]).strip() for i in items if i.get("verified_by"))
    return {
        "golden_file": _display(golden),
        "ok": not errors,
        "total_items": len(items),
        "items_with_errors": len(bad_items),
        "quotes_checked": check_quotes,
        "quotas": quotas,
        "items_by_author": dict(sorted(authors.items())),
        "items_by_verifier": dict(sorted(verifiers.items())),
        "errors": errors,
        "warnings": warnings,
    }


def print_report(result: dict) -> None:
    print(f"golden set: {result['golden_file']}")
    if "quotas" in result:
        print(f"items: {result['total_items']} (minimum {MIN_TOTAL})")
        print(f"  {'category':<14}{'count':>6}{'min':>6}{'target':>8}")
        for cat, q in result["quotas"].items():
            mark = "ok" if q["count"] >= q["minimum"] else "LOW"
            print(f"  {cat:<14}{q['count']:>6}{q['minimum']:>6}{q['target']:>8}  {mark}")
        print(f"by author:   {result['items_by_author']}")
        print(f"by verifier: {result['items_by_verifier']}")
    for w in result["warnings"]:
        print(f"WARNING {w}")
    for e in result["errors"]:
        print(f"ERROR   {e}")
    print("PASS" if result["ok"] else f"FAIL: {len(result['errors'])} error(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--golden", type=Path, default=paths.GOLDEN)
    parser.add_argument("--normalized", type=Path, default=paths.NORMALIZED)
    parser.add_argument("--out", type=Path, default=paths.ARTIFACTS / "golden_check.json")
    parser.add_argument(
        "--no-quotes",
        action="store_true",
        help="skip the quote-on-page check (only before data/normalized/ exists; recorded)",
    )
    args = parser.parse_args(argv)

    result = run(args.golden, args.normalized, check_quotes=not args.no_quotes)
    print_report(result)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
