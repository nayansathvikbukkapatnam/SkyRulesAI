"""Show golden items next to the saved page text, for the people verifying them.

Read-only: it prints and never edits eval/golden.jsonl. You still open the live
page yourself and set verified_by and verified_date by hand.

    python -m src.eval.show_item g003            # one item
    python -m src.eval.show_item --name Pavan    # every item assigned to Pavan

For each evidence quote it says whether the quote is found on the saved page and
prints the text around it (lowercased: quote matching ignores case, quotes,
dashes and spacing, and nothing else). For unanswerable items it prints what the
author says they searched for and a grep command that repeats the search.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.common import paths
from src.eval.golden_check import PageStore, load_items, normalize_text

ASSIGNMENTS = paths.EVAL / "verification_assignments.json"
CONTEXT = 260


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def load_assignments(path: Path = ASSIGNMENTS) -> dict[str, list[str]]:
    return json.loads(path.read_text(encoding="utf-8"))["assignments"]


def render(item: dict, pages: PageStore) -> str:
    lines = [
        "=" * 78,
        f"{item['id']}  [{item['category']}]  author: {item.get('author')}  "
        f"verified_by: {item.get('verified_by') or '-'}",
        f"QUESTION: {item['question']}",
    ]
    if item.get("gold_answer"):
        lines.append(f"GOLD:     {item['gold_answer']}")
    urls, quotes = _as_list(item.get("source_url")), _as_list(item.get("evidence_quote"))
    for n, (url, quote) in enumerate(zip(urls, quotes, strict=False), start=1):
        text = pages.text_for(url)
        lines += ["", f"SOURCE {n}: {url}", f"QUOTE:    {quote}"]
        if text is None:
            lines.append("  -> this page is NOT in data/normalized/")
            continue
        at = text.find(normalize_text(quote))
        if at < 0:
            lines.append("  -> quote NOT FOUND on the saved page")
            continue
        start, end = max(0, at - CONTEXT), at + len(normalize_text(quote)) + CONTEXT
        lines.append("  -> found on the saved page. Context (lowercased):")
        lines.append(f"  ...{text[start:end]}...")
    if item.get("searched_for"):
        lines += ["", f"SEARCHED: {item['searched_for']}"]
        lines.append('To repeat a search:  grep -ril "<term>" data/normalized | wc -l')
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", nargs="*", help="item ids such as g003")
    parser.add_argument("--name", help="show every item assigned to this person")
    parser.add_argument("--golden", type=Path, default=paths.GOLDEN)
    parser.add_argument("--normalized", type=Path, default=paths.NORMALIZED)
    args = parser.parse_args(argv)

    wanted = list(args.ids)
    if args.name:
        assigned = load_assignments()
        match = {k.casefold(): v for k, v in assigned.items()}.get(args.name.casefold())
        if match is None:
            print(f"no assignments for {args.name!r}; known: {sorted(assigned)}")
            return 1
        wanted += match
    if not wanted:
        parser.error("give item ids or --name")

    items, _ = load_items(args.golden)
    by_id = {i["id"]: i for i in items}
    missing = [w for w in wanted if w not in by_id]
    if missing:
        print(f"not in {args.golden}: {missing}")
        return 1
    pages = PageStore(args.normalized)
    print("\n\n".join(render(by_id[w], pages) for w in wanted))
    return 0


if __name__ == "__main__":
    sys.exit(main())
