"""OCR tests with a mocked Tesseract: CI never needs the Tesseract program."""

import pytest

from src.extract import ocr
from src.extract import pdf as pdf_x


def tesseract_data(rows):
    """Build an image_to_data dict from (block, par, line, word, conf) rows."""
    keys = ("block_num", "par_num", "line_num", "text", "conf")
    return {k: [r[i] for r in rows] for i, k in enumerate(keys)}


def test_lines_keep_confident_words_in_reading_order():
    data = tesseract_data(
        [
            (1, 1, 1, "Know", 96),
            (1, 1, 1, "Before", 91),
            (1, 1, 1, "@#%", 12),  # low confidence: dropped
            (1, 1, 2, "You", 88),
            (1, 1, 2, "Fly", 90),
            (1, 1, 2, "", -1),  # layout entries have no text
            (2, 1, 1, "Register", 77),
        ]
    )
    assert ocr.lines_from_data(data) == ["Know Before", "You Fly", "Register"]


class FakeImage:
    original = object()


class FakePage:
    def to_image(self, resolution):
        assert resolution == ocr.RESOLUTION
        return FakeImage()


def test_ocr_page_renders_the_page_and_reads_it(monkeypatch):
    data = tesseract_data([(1, 1, 1, "Fly", 95), (1, 1, 1, "safely", 95)])
    monkeypatch.setattr(ocr.pytesseract, "image_to_data", lambda image, output_type: data)
    assert ocr.ocr_page(FakePage()) == ["Fly safely"]


@pytest.fixture
def tesseract(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    return monkeypatch


def test_textless_page_gets_ocr_lines_without_a_font_size(tesseract):
    tesseract.setattr(
        ocr, "ocr_page", lambda page: ["Always fly below 400 feet", "Keep it in sight"]
    )
    warnings = []
    lines = pdf_x._ocr_lines(FakePage(), 3, warnings)
    assert [ln["text"] for ln in lines] == ["Always fly below 400 feet", "Keep it in sight"]
    assert all(ln["size"] == 0 and ln["bold"] is False for ln in lines)
    assert warnings == []


def test_ocr_failure_is_logged_not_raised(tesseract):
    def boom(page):
        raise RuntimeError("tesseract crashed")

    tesseract.setattr(ocr, "ocr_page", boom)
    warnings = []
    assert pdf_x._ocr_lines(FakePage(), 4, warnings) == []
    assert warnings == ["page 4: OCR failed (RuntimeError)"]


def test_ocr_with_too_little_confident_text_counts_as_empty(tesseract):
    tesseract.setattr(ocr, "ocr_page", lambda page: ["ok"])
    warnings = []
    assert pdf_x._ocr_lines(FakePage(), 5, warnings) == []
    assert warnings == ["page 5: OCR found no confident text"]


def test_without_tesseract_the_page_is_reported_as_needing_ocr(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: False)
    warnings = []
    assert pdf_x._ocr_lines(FakePage(), 6, warnings) == []
    assert warnings == ["page 6: no text layer (needs OCR; Tesseract not installed)"]


def test_available_is_false_when_no_tesseract_program_is_found(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, "_available", None)
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    monkeypatch.setattr(ocr, "WINDOWS_DEFAULT", tmp_path / "missing.exe")
    assert ocr.available() is False
