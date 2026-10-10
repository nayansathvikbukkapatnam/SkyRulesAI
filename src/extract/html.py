"""HTML to sections: trafilatura for ordinary web pages, a structure walker otherwise.

trafilatura (Markdown output, headings as #, tables kept) handles normal pages
such as Know Before You Fly well. On the eCFR and Federal Register API responses
it drops headings and guesses wrong titles and dates, so those use the
BeautifulSoup walker below, which keeps every heading level and table. The
walker is also the fallback when trafilatura returns too little text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import trafilatura
from bs4 import BeautifulSoup, Tag

from src.common.contracts import Section

HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
BLOCKS = ("p", "li", "pre", "blockquote", "dd", "dt")
MIN_TRAFILATURA_CHARS = 200


@dataclass
class Extracted:
    title: str | None
    sections: list[Section]
    method: str
    date: str | None = None  # a date the extractor found in page metadata, if any
    warnings: list[str] = field(default_factory=list)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def table_to_markdown(table: Tag) -> str | None:
    """A Markdown table; the caption (if any) goes on the line above it."""
    rows = []
    for tr in table.find_all("tr"):
        cells = [
            clean(c.get_text(" ", strip=True)).replace("|", "\\|")
            for c in tr.find_all(["th", "td"])
        ]
        if any(cells):
            rows.append(cells)
    if len(rows) < 2 or max(len(r) for r in rows) < 2:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    caption = table.find("caption")
    if caption and clean(caption.get_text(" ")):
        lines.insert(0, clean(caption.get_text(" ")) + "\n")
    return "\n".join(lines)


class _Sections:
    """Accumulates text and tables under the current heading path."""

    def __init__(self, title: str | None) -> None:
        self.root = [title] if title else []
        self.stack: list[tuple[int, str]] = []
        self.sections: list[Section] = []
        self._text: list[str] = []
        self._tables: list[str] = []

    def heading(self, level: int, text: str) -> None:
        self.flush()
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        self.stack.append((level, text))

    def text(self, text: str) -> None:
        if text:
            self._text.append(text)

    def table(self, markdown: str) -> None:
        self._tables.append(markdown)

    def flush(self) -> None:
        if self._text or self._tables:
            path = self.root + [t for _, t in self.stack]
            self.sections.append(
                Section(heading_path=path, text="\n\n".join(self._text), tables=self._tables)
            )
        self._text, self._tables = [], []

    def done(self) -> list[Section]:
        self.flush()
        return self.sections


def walk(root: Tag, title: str | None, skip_headings: re.Pattern | None = None) -> list[Section]:
    """Sections in document order, one per heading, with tables kept as Markdown.

    A heading matching `skip_headings` drops everything until the next heading of
    the same or higher level (used for contact blocks and tables of contents).
    """
    out = _Sections(title)
    skipping_at: int | None = None
    for el in root.find_all([*HEADINGS, *BLOCKS, "table"]):
        if el.name != "table" and el.find_parent("table"):
            continue
        if el.name in BLOCKS and el.find_parent(list(BLOCKS)):
            continue  # its text is already part of the enclosing block
        if el.name in HEADINGS:
            level, text = int(el.name[1]), clean(el.get_text(""))
            if not text:
                continue
            if skipping_at is not None and level <= skipping_at:
                skipping_at = None
            if skip_headings and skip_headings.search(text):
                out.flush()
                skipping_at = level
                continue
            if skipping_at is None:
                out.heading(level, text)
        elif skipping_at is not None:
            continue
        elif el.name == "table":
            markdown = table_to_markdown(el)
            if markdown:
                out.table(markdown)
        else:
            # "" keeps the page's own spacing; " " would turn <span>(</span>a) into "( a )".
            out.text(clean(el.get_text("")))
    return out.done()


def _soup(raw: bytes) -> BeautifulSoup:
    soup = BeautifulSoup(raw, "html.parser")
    for el in soup.find_all(["script", "style", "noscript", "svg", "template"]):
        el.decompose()
    return soup


# --- eCFR renderer API ---------------------------------------------------------


def ecfr_section(raw: bytes) -> tuple[Extracted, str | None]:
    """One eCFR section. Returns the extraction and the section's public path."""
    soup = _soup(raw)
    h4 = soup.find("h4")
    title, public_path = None, None
    if h4 is not None:
        title = clean(h4.get_text(""))
        meta = h4.get("data-hierarchy-metadata") or ""
        m = re.search(r'"path"\s*:\s*"([^"]+)"', meta)
        public_path = m.group(1) if m else None
        h4.decompose()
    title = f"14 CFR {title}" if title else None
    sections = walk(soup, title)
    warnings = (
        ["reserved section (no text)"] if not sections and "[Reserved]" in (title or "") else []
    )
    return Extracted(title, sections, "bs4-ecfr", warnings=warnings), public_path


# --- Federal Register full_text API --------------------------------------------

# The contact block names an FAA employee with a phone and email (rule 2: no PII);
# the table of contents only repeats headings.
FR_SKIP_HEADINGS = re.compile(r"^(for further information contact|table of contents)\b", re.I)


def federal_register(raw: bytes, title: str | None) -> Extracted:
    soup = _soup(raw)
    # Page-number markers, "Document Headings" help boxes and other site chrome.
    for el in soup.select(".unprinted-element, .fr-box, .document-headings, .printed-page"):
        el.decompose()
    sections = walk(soup, title, skip_headings=FR_SKIP_HEADINGS)
    return Extracted(title, sections, "bs4-federalregister")


# --- Ordinary web pages --------------------------------------------------------


def _markdown_sections(markdown: str, title: str | None) -> list[Section]:
    out = _Sections(title)
    table: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("|"):
            table.append(line)
            continue
        if table:
            out.table("\n".join(table))
            table = []
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            text = clean(m.group(2))
            if text:
                out.heading(len(m.group(1)), text)
        elif clean(line):
            out.text(clean(line))
    if table:
        out.table("\n".join(table))
    return out.done()


def web_page(raw: bytes) -> Extracted:
    html = raw.decode("utf-8", errors="replace")
    meta = trafilatura.extract_metadata(html)
    title = clean(meta.title) if meta and meta.title else None
    if title:
        title = re.sub(r"\s+[-|–]\s+Know Before You Fly$", "", title)
    date = meta.date if meta and meta.date else None
    markdown = trafilatura.extract(
        html,
        output_format="markdown",
        include_tables=True,
        include_links=False,
        include_images=False,
        favor_recall=True,
    )
    if markdown and len(markdown) >= MIN_TRAFILATURA_CHARS:
        return Extracted(title, _markdown_sections(markdown, title), "trafilatura", date)
    soup = _soup(raw)
    for el in soup.find_all(["nav", "header", "footer", "aside", "form"]):
        el.decompose()
    body = soup.find("main") or soup.body or soup
    warning = f"trafilatura returned {len(markdown or '')} chars; used bs4 fallback"
    return Extracted(title, walk(body, title), "bs4", date, [warning])
