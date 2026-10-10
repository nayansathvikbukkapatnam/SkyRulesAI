"""PDF to sections with pdfplumber: column-aware text, tables as Markdown, font-size headings.

Federal Register PDFs are set in three columns with sideways print-production
text in the margin, so each page is split at its column gutters (vertical
strips that almost no word crosses) and read column by column; rotated words
are dropped. Headings are lines noticeably larger than the body text, or short
bold lines. Pages with no text layer are read with OCR (src/extract/ocr.py, task A3)
when Tesseract is installed, and reported as needing OCR otherwise.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from statistics import median

import pdfplumber

from src.common.contracts import Section
from src.extract import ocr
from src.extract.html import Extracted, clean

MIN_PAGE_CHARS = 20
GUTTER_MIN_WIDTH = 6
LINE_TOLERANCE = 3
# Federal Register running heads and footers, repeated on every page.
BOILERPLATE = re.compile(
    r"^(Federal Register\s*/\s*Vol\.|VerDate|jspears on|\d{4,5}\s*$|Jkt \d+)", re.I
)


def _cell(value) -> str:
    return clean(str(value or "")).replace("|", "\\|")


def table_to_markdown(rows: list[list]) -> str | None:
    rows = [[_cell(c) for c in r] for r in rows if r and any(_cell(c) for c in r)]
    if len(rows) < 2 or max(len(r) for r in rows) < 2:
        return None
    width = max(len(r) for r in rows)
    filled = sum(1 for r in rows for c in r if c)
    if filled < 0.5 * width * len(rows):
        return None  # layout boxes detected as tables, not real data tables
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    return "\n".join(lines + ["| " + " | ".join(r) + " |" for r in rows[1:]])


def _columns(words: list[dict], width: float) -> list[tuple[float, float]]:
    """x ranges of the text columns: split at gutters that almost no word crosses."""
    if not words:
        return [(0, width)]
    diff = [0] * (int(width) + 3)
    last = len(diff) - 1
    for w in words:  # clamp: words in a print bleed can sit outside the page box
        diff[min(last, max(0, int(w["x0"])))] += 1
        diff[min(last, max(0, int(w["x1"]) + 1))] -= 1
    coverage, running = [], 0
    for d in diff:
        running += d
        coverage.append(running)
    n_lines = len({round(w["top"]) for w in words})
    limit = max(1, int(0.03 * n_lines))
    left, right = int(0.1 * width), int(0.9 * width)
    cuts, start = [], None
    for x in range(left, right):
        if coverage[x] <= limit:
            start = x if start is None else start
        elif start is not None:
            if x - start >= GUTTER_MIN_WIDTH:
                cuts.append((start + x) / 2)
            start = None
    edges = [0.0, *cuts, width]
    return list(zip(edges, edges[1:], strict=False))


def _lines(words: list[dict]) -> list[dict]:
    """Group words into lines: text, median font size, and whether it is all bold."""
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= LINE_TOLERANCE:
            lines[-1].append(w)
        else:
            lines.append([w])
    out = []
    for ws in lines:
        ws.sort(key=lambda w: w["x0"])
        out.append(
            {
                "text": clean(" ".join(w["text"] for w in ws)),
                "size": median(w["size"] for w in ws),
                "bold": all("bold" in (w.get("fontname") or "").lower() for w in ws),
            }
        )
    return out


def _inside(word: dict, boxes: list[tuple]) -> bool:
    return any(
        b[0] <= word["x0"] and word["x1"] <= b[2] and b[1] <= word["top"] <= b[3] for b in boxes
    )


def _ocr_lines(page, number: int, warnings: list[str]) -> list[dict]:
    """OCR a page with no text layer. Failures are logged as warnings, never raised."""
    if not ocr.available():
        warnings.append(f"page {number}: no text layer (needs OCR; Tesseract not installed)")
        return []
    try:
        texts = ocr.ocr_page(page)
    except Exception as e:  # a bad page must not stop the document
        warnings.append(f"page {number}: OCR failed ({type(e).__name__})")
        return []
    if sum(len(t) for t in texts) < MIN_PAGE_CHARS:
        warnings.append(f"page {number}: OCR found no confident text")
        return []
    # OCR gives no font size; size 0 keeps these lines out of heading detection.
    return [{"text": clean(t), "size": 0, "bold": False} for t in texts]


def extract_pdf(raw: bytes, title_hint: str | None = None, use_ocr: bool = True) -> Extracted:
    warnings: list[str] = []
    ocr_pages = 0
    page_lines: list[list[dict]] = []
    page_tables: list[list[str]] = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        meta_title = clean(pdf.metadata.get("Title") or "") or None
        date = pdf.metadata.get("ModDate") or pdf.metadata.get("CreationDate")
        for number, page in enumerate(pdf.pages, 1):
            found = page.find_tables()
            boxes = [t.bbox for t in found]
            tables = [md for t in found if (md := table_to_markdown(t.extract()))]
            words = [
                w
                for w in page.extract_words(
                    extra_attrs=["size", "fontname"], keep_blank_chars=False
                )
                if w.get("upright", True) and not _inside(w, boxes)
            ]
            lines = []
            if sum(len(w["text"]) for w in words) < MIN_PAGE_CHARS and not tables:
                lines = _ocr_lines(page, number, warnings) if use_ocr else []
                if lines:
                    ocr_pages += 1
                elif not use_ocr:
                    warnings.append(f"page {number}: no text layer (needs OCR)")
            for x0, x1 in _columns(words, page.width):
                column = [w for w in words if x0 <= (w["x0"] + w["x1"]) / 2 < x1]
                lines += [ln for ln in _lines(column) if not BOILERPLATE.search(ln["text"])]
            page_lines.append(lines)
            page_tables.append(tables)

    sizes = Counter(
        round(ln["size"])
        for lines in page_lines
        for ln in lines
        if ln["size"] > 0
        for _ in ln["text"]
    )
    body = sizes.most_common(1)[0][0] if sizes else 10
    title = title_hint or meta_title
    if not title:
        first = [ln for ln in (page_lines[0] if page_lines else []) if len(ln["text"]) > 3]
        title = max(first, key=lambda ln: ln["size"])["text"] if first else None

    root = [title] if title else []
    sections: list[Section] = []
    heading: list[str] = []
    paragraph: list[str] = []
    texts: list[str] = []
    tables: list[str] = []

    def flush() -> None:
        nonlocal texts, tables
        if paragraph:
            texts.append(_join(paragraph))
            paragraph.clear()
        if texts or tables:
            sections.append(
                Section(heading_path=root + heading, text="\n\n".join(texts), tables=tables)
            )
        texts, tables = [], []

    for lines, page_tbls in zip(page_lines, page_tables, strict=True):
        for ln in lines:
            text = ln["text"]
            is_heading = (
                len(text) <= 90
                and re.search(r"[A-Za-z]{3}", text)
                and (
                    ln["size"] >= body * 1.15 or (ln["bold"] and not text.endswith((".", ",", ";")))
                )
            )
            if is_heading:
                flush()
                heading = [text]
            elif text:
                paragraph.append(text)
                if text.endswith((".", ":", ";", "?", "!")) and len(text) < 60:
                    texts.append(_join(paragraph))  # a short last line ends a paragraph
                    paragraph.clear()
        tables.extend(page_tbls)
    flush()
    method = "pdfplumber+tesseract" if ocr_pages else "pdfplumber"
    return Extracted(title, sections, method, _pdf_date(date), warnings, ocr_pages)


def _join(lines: list[str]) -> str:
    """Join wrapped lines, mending words hyphenated across a line break."""
    text = lines[0]
    for line in lines[1:]:
        if text.endswith("-") and line[:1].islower():
            text = text[:-1] + line
        else:
            text += " " + line
    return clean(text)


def _pdf_date(value) -> str | None:
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", str(value or ""))
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None
