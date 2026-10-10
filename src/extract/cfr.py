"""One CFR section from govinfo's annual-edition section XML (a CFRGRANULE).

The XML is structured: <SECTNO>, <SUBJECT> (or <RESERVED>), paragraphs, an
optional <GPOTABLE>, and a <CITA> source note; <ANCESTORS> names the part and
subpart. That is far cleaner than the section PDFs, which print whole pages.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from src.common.contracts import Section
from src.extract.html import Extracted, clean

SKIP_TAGS = {"SECTNO", "SUBJECT", "RESERVED", "GPOTABLE"}


def _text(el: ET.Element) -> str:
    return clean("".join(el.itertext()))


def _table(el: ET.Element) -> str | None:
    rows = [
        [_text(c).replace("|", "\\|") for c in row]
        for row in el.iter()
        if row.tag in ("BOXHD", "ROW")
    ]
    rows = [r for r in rows if any(r)]
    if len(rows) < 2:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    return "\n".join(lines + ["| " + " | ".join(r) + " |" for r in rows[1:]])


def cfr_section(raw: bytes) -> tuple[Extracted, str | None, str | None]:
    """Returns (extraction, section number e.g. "107.31", edition date e.g. "2025-01-01")."""
    root = ET.fromstring(raw)
    section = root if root.tag == "SECTION" else root.find(".//SECTION")
    date = root.findtext(".//FDSYS/DATE")
    if section is None:
        return Extracted(None, [], "xml-cfr", date, ["no SECTION element"]), None, date
    number = clean((section.findtext("SECTNO") or "").replace("§", ""))
    subject = clean(section.findtext("SUBJECT") or "")
    reserved = section.find("RESERVED") is not None or subject == "[Reserved]"
    year = (date or "")[:4]
    title = f"14 CFR § {number} {subject}".strip() + (f" ({year} edition)" if year else "")
    if reserved:
        return Extracted(title, [], "xml-cfr", date, ["reserved section (no text)"]), number, date
    texts = [_text(el) for el in section if el.tag not in SKIP_TAGS]
    tables = [t for el in section.iter("GPOTABLE") if (t := _table(el))]
    sections = [
        Section(heading_path=[title], text="\n\n".join(t for t in texts if t), tables=tables)
    ]
    return Extracted(title, sections, "xml-cfr", date), number, date
