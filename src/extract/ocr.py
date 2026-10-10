"""OCR for PDF pages that have no text layer (task A3).

Some Know Before You Fly handouts are pictures of text, so pdfplumber finds no
words on those pages. Each such page is rendered at 300 dpi and read with
Tesseract. Only words Tesseract is reasonably sure of (confidence >= 60) are
kept, grouped back into lines, so a noisy poster does not add garbage text.

Tesseract is a separate program (pytesseract only calls it). When it is not
installed, as in CI, `available()` is False and extraction behaves as before:
the page is reported as needing OCR.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytesseract

RESOLUTION = 300
MIN_CONFIDENCE = 60
WINDOWS_DEFAULT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")

_available: bool | None = None


def available() -> bool:
    """True when the Tesseract program can be run (checked once)."""
    global _available
    if _available is None:
        command = shutil.which("tesseract") or (
            str(WINDOWS_DEFAULT) if WINDOWS_DEFAULT.exists() else None
        )
        if command:
            pytesseract.pytesseract.tesseract_cmd = command
            try:
                pytesseract.get_tesseract_version()
                _available = True
            except Exception:
                _available = False
        else:
            _available = False
    return _available


def version() -> str | None:
    return str(pytesseract.get_tesseract_version()) if available() else None


def lines_from_data(data: dict, min_confidence: int = MIN_CONFIDENCE) -> list[str]:
    """Rebuild text lines from pytesseract.image_to_data output, keeping confident words."""
    lines: dict[tuple, list[str]] = {}
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        if not word or float(data["conf"][i]) < min_confidence:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
    return [" ".join(words) for _, words in sorted(lines.items())]


def ocr_page(page) -> list[str]:
    """OCR one pdfplumber page; returns its text lines (may be empty)."""
    image = page.to_image(resolution=RESOLUTION).original
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    return lines_from_data(data)
