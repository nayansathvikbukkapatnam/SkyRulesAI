"""Extraction tests on small hand-made HTML snippets (fixtures, not FAA content)."""

import json

import pytest

from src.common.contracts import validate_normalized_doc
from src.extract import html as html_x
from src.extract.normalize import FR_CONTACT, _effective_date, normalize, trim_cfr_section
from src.extract.pdf import _columns, _join, _pdf_date, table_to_markdown
from src.extract.pii import redact

ECFR_HTML = b"""<div class="section" id="107.31">
<h4 data-hierarchy-metadata='{"path":"/current/title-14/section-107.31"}'>
\xc2\xa7 107.31 Visual line of sight aircraft operation.</h4>
<div id="p-107.31(a)"><p class="indent-1"><span class="paragraph-hierarchy"
><span class="paren">(</span>a<span class="paren">)</span></span>
Fixture text about seeing the aircraft, see <a href="/x">paragraph (b)</a> below.</p></div>
<script type="application/json">{"origins": [{"source": ["not content"]}]}</script>
</div>"""

FR_HTML = b"""<div class="preamble">
<div class="document-headings"><div class="fr-box">
<p>Document headings vary by document type.</p></div></div>
<h1 id="h-1">AGENCY:</h1><p>Federal Aviation Administration (FAA), DOT.</p>
<h1 id="h-4">DATES:</h1><p>This rule is effective March 16, 2021.</p>
<h1 id="h-5">FOR FURTHER INFORMATION CONTACT:</h1>
<p>Jane Fixture, Office of Rulemaking; telephone (202) 555-0100;
email jane.fixture@example.gov.</p>
</div>
<div class="supplemental-info">
<h1 id="h-6">SUPPLEMENTARY INFORMATION:</h1>
<h2>Table of Contents</h2><p>I. Executive Summary</p>
<h2>I. Executive Summary</h2>
<p>Fixture summary text.
<span class="printed-page-inline unprinted-element">(printed page 4391)</span></p>
<h3>A. Costs</h3>
<table><caption><p>Table 1&mdash;Fixture Costs</p></caption>
<tr><th>Scenario</th><th>Cost</th></tr><tr><td>Base</td><td>230.69</td></tr></table>
<ul><li><p>Nested paragraph in a list item</p></li></ul>
</div>"""

WEB_HTML = b"""<html><head><title>Fly for Work - Know Before You Fly</title>
<meta property="article:modified_time" content="2026-03-30T10:00:00+00:00"></head>
<body><nav>Home | About | Contact</nav><main><article>
<h2>What is commercial use?</h2>
<p>Fixture text. Commercial use of a drone is any flight for compensation or for a business,
which means the operator must follow the rules for commercial flights and hold a certificate.</p>
<h2>What do I need?</h2>
<p>Fixture text. You need a remote pilot certificate, a registered drone and a plan for each
flight, and you must keep the drone within visual line of sight at all times when flying.</p>
</article></main><footer>Footer links</footer></body></html>"""


# --- HTML -------------------------------------------------------------------


def test_ecfr_section_keeps_title_public_path_and_paragraph_labels():
    ext, public_path = html_x.ecfr_section(ECFR_HTML)
    assert public_path == "/current/title-14/section-107.31"
    assert ext.title == "14 CFR § 107.31 Visual line of sight aircraft operation."
    [section] = ext.sections
    assert section["heading_path"] == [ext.title]
    assert section["text"].startswith("(a) Fixture text")  # not "( a )"
    assert "paragraph (b) below" in section["text"]
    assert "not content" not in section["text"]  # the script JSON is stripped


def test_reserved_ecfr_section_is_flagged_not_failed():
    raw = (
        b'<div class="section"><h4 data-hierarchy-metadata=\'{"path":"/x/section-107.5"}\'>'
        b"\xc2\xa7 107.5 [Reserved]</h4></div>"
    )
    ext, _ = html_x.ecfr_section(raw)
    assert ext.sections == []
    assert ext.warnings == ["reserved section (no text)"]


def test_federal_register_keeps_headings_and_tables_and_drops_contact_and_chrome():
    ext = html_x.federal_register(FR_HTML, "Fixture Rule")
    paths = [s["heading_path"] for s in ext.sections]
    assert ["Fixture Rule", "AGENCY:"] in paths
    assert ["Fixture Rule", "SUPPLEMENTARY INFORMATION:", "I. Executive Summary"] in paths
    assert [
        "Fixture Rule",
        "SUPPLEMENTARY INFORMATION:",
        "I. Executive Summary",
        "A. Costs",
    ] in paths
    text = json.dumps(ext.sections)
    assert "Jane Fixture" not in text and "555-0100" not in text  # contact block dropped
    assert "Table of Contents" not in text
    assert "printed page" not in text and "Document headings vary" not in text
    costs = next(s for s in ext.sections if s["heading_path"][-1] == "A. Costs")
    assert costs["tables"] == [
        "Table 1—Fixture Costs\n\n| Scenario | Cost |\n| --- | --- |\n| Base | 230.69 |"
    ]
    assert costs["text"].count("Nested paragraph") == 1  # li > p not counted twice


def test_web_page_uses_trafilatura_headings_title_and_date():
    ext = html_x.web_page(WEB_HTML)
    assert ext.method == "trafilatura"
    assert ext.title == "Fly for Work"
    assert ext.date == "2026-03-30"
    assert [s["heading_path"] for s in ext.sections] == [
        ["Fly for Work", "What is commercial use?"],
        ["Fly for Work", "What do I need?"],
    ]
    assert "Footer links" not in json.dumps(ext.sections)


def test_web_page_falls_back_to_bs4_when_trafilatura_finds_too_little():
    raw = b"<html><body><main><h2>Short</h2><p>Tiny fixture page.</p></main></body></html>"
    ext = html_x.web_page(raw)
    assert ext.method == "bs4"
    assert ext.warnings and "bs4 fallback" in ext.warnings[0]
    assert ext.sections[0]["text"] == "Tiny fixture page."


# --- PDF helpers -------------------------------------------------------------


def _word(x0, x1, top, text="word"):
    return {"x0": x0, "x1": x1, "top": top, "text": text, "size": 9, "fontname": "Times"}


def test_columns_split_at_gutters_and_keep_single_column_pages_whole():
    three = [_word(x, x + 150, top) for top in range(0, 600, 12) for x in (20, 210, 400)]
    cols = _columns(three, 600)
    assert len(cols) == 3
    assert cols[0][0] == 0 and cols[-1][1] == 600
    one = [_word(20, 580, top) for top in range(0, 600, 12)]
    assert _columns(one, 600) == [(0.0, 600)]


def test_columns_tolerate_words_outside_the_page_box():
    bleed = [_word(-15, 40, 10), _word(590, 640, 20), _word(700, 760, 30)]
    assert _columns(bleed, 600)[0][0] == 0


def test_join_mends_hyphenated_line_breaks():
    assert _join(["The remote pilot must main-", "tain visual line of sight."]) == (
        "The remote pilot must maintain visual line of sight."
    )
    assert _join(["Part 107-", "Small UAS"]) == "Part 107- Small UAS"


def test_pdf_tables_drop_sparse_layout_boxes():
    assert table_to_markdown([["A", "B"], ["1", "2"]]) == "| A | B |\n| --- | --- |\n| 1 | 2 |"
    assert table_to_markdown([["A", None, None], [None, None, None], ["", "", "x"]]) is None


def test_pdf_date():
    assert _pdf_date("D:20251231144450Z00'00'") == "2025-12-31"
    assert _pdf_date(None) is None


# --- Dates and PII -----------------------------------------------------------


def test_effective_date_comes_from_dates_section_else_publication_date():
    dated = [
        {
            "heading_path": ["T", "DATES:"],
            "text": "This rule is effective March 16, 2021.",
            "tables": [],
        }
    ]
    assert _effective_date(dated, "2021-01-15") == "2021-03-16"
    proposed = [
        {"heading_path": ["T", "DATES:"], "text": "Send comments by March 2, 2020.", "tables": []}
    ]
    assert _effective_date(proposed, "2019-12-31") == "2019-12-31"


def test_cfr_section_is_trimmed_to_its_own_text_and_caption_mended():
    t = "14 CFR § 107.31"

    def sec(heading, text, tables=()):
        return {"heading_path": [t, *heading], "text": text, "tables": list(tables)}

    sections = [
        sec([], "end of 107.29 from the same page."),
        sec(["§107.31 Visual line of sight aircraft op-"], "eration.\n\n(a) With vision unaided."),
        sec(["Note to paragraph (a)"], "A note.", ["| a | b |"]),
        sec(["§107.35 Operation of multiple small"], "unmanned aircraft. Next section."),
    ]
    caption, [section] = trim_cfr_section(sections, "107.31")
    assert caption == "Visual line of sight aircraft operation."
    assert section["heading_path"] == [t]
    assert section["text"] == "(a) With vision unaided.\n\nNote to paragraph (a)\n\nA note."
    assert section["tables"] == ["| a | b |"]
    assert "107.29" not in section["text"] and "Next section" not in section["text"]


def test_cfr_section_without_its_heading_is_left_whole():
    sections = [{"heading_path": ["x"], "text": "no heading here", "tables": []}]
    assert trim_cfr_section(sections, "107.31") == (None, sections)


def test_reserved_cfr_section_caption():
    sections = [{"heading_path": ["x", "§ 107.5 [Reserved]"], "text": "", "tables": []}]
    caption, _ = trim_cfr_section(sections, "107.5")
    assert caption == "[Reserved]"


def test_pdf_contact_block_is_removed_up_to_supplementary_information():
    text = (
        "DATES: Effective March 16, 2021. FOR FURTHER INFORMATION CONTACT: Jane Fixture, "
        "Office of Rulemaking. SUPPLEMENTARY INFORMATION: Background."
    )
    assert (
        FR_CONTACT.sub("", text)
        == "DATES: Effective March 16, 2021. SUPPLEMENTARY INFORMATION: Background."
    )


@pytest.mark.parametrize(
    "text, kind",
    [
        ("write to jane.fixture@example.gov today", "email"),
        ("call (202) 555-0100", "phone"),
        ("call 202-555-0100", "phone"),
        ("call 1-844-555-0100", "phone"),
        ("at 800 Independence Avenue SW, Washington", "street_address"),
        ("at 1200 New Jersey Avenue SE", "street_address"),
    ],
)
def test_pii_redaction(text, kind):
    redacted, counts = redact(text)
    assert counts == {kind: 1}
    assert f"[REDACTED {kind.upper()}]" in redacted


@pytest.mark.parametrize(
    "text",
    [
        "14 CFR 107.31 applies",
        "Docket No. FAA-2019-1100; Amdt. No. 107-7",
        "81 FR 42209, June 28, 2016",
        "fly below 400 feet above ground level",
        "Docket FAA-2015-0150",
    ],
)
def test_pii_redaction_leaves_regulatory_numbers_alone(text):
    assert redact(text) == (text, {})


# --- End to end --------------------------------------------------------------


def test_normalize_writes_valid_docs_stats_and_counts_only_pii_log(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    files = {
        "ecfr": (
            "https://www.ecfr.gov/api/renderer/v1/content/enhanced/current/title-14?section=107.31",
            ECFR_HTML,
        ),
        "fr": (
            "https://www.federalregister.gov/documents/full_text/html/2021/01/15/2020-28948.html",
            FR_HTML,
        ),
        "web": ("https://knowbeforeyoufly.org/whos-flying/commercial", WEB_HTML),
        "copy": ("https://knowbeforeyoufly.org/whos-flying/commercial-copy", WEB_HTML + b" "),
    }
    lines = []
    for name, (url, body) in files.items():
        path = raw_dir / f"{name}.bin"
        path.write_bytes(body)
        lines.append(
            json.dumps(
                {
                    "url": url,
                    "final_url": url,
                    "fetched_at": "2026-10-10T19:00:00+00:00",
                    "status": 200,
                    "content_type": "text/html",
                    "sha256": name,
                    "path": path.as_posix(),
                    "robots_allowed": True,
                }
            )
        )
    manifest = raw_dir / "manifest.jsonl"
    manifest.write_text("\n".join(lines) + "\n")
    out, artifacts = tmp_path / "normalized", tmp_path / "artifacts"

    stats = normalize(manifest, out, artifacts)

    docs = {d["source_url"]: d for d in (json.loads(p.read_text()) for p in out.glob("*.json"))}
    assert set(docs) == {  # eCFR is a disabled source, so its file is never used
        "https://www.federalregister.gov/d/2020-28948",
        "https://knowbeforeyoufly.org/whos-flying/commercial",
    }
    for doc in docs.values():
        assert validate_normalized_doc(doc) == []
    fr = docs["https://www.federalregister.gov/d/2020-28948"]
    assert (
        fr["title"]
        == "Remote Identification of Unmanned Aircraft (Federal Register Rule, 2021-01-15)"
    )
    assert fr["effective_date"] == "2021-03-16"
    assert docs["https://knowbeforeyoufly.org/whos-flying/commercial"]["page_type"] == "guide_page"

    assert stats["documents_written"] == 2
    assert stats["excluded_disabled_sources"] == {"www.ecfr.gov": 1}
    assert stats["duplicates_removed"] == 1  # the copy has the same extracted content
    assert stats["documents_with_tables"] == 1
    saved = json.loads((artifacts / "ingestion_stats.json").read_text())
    assert saved["documents_written"] == 2
    pii_log = (artifacts / "pii_log.json").read_text()
    assert "jane" not in pii_log.lower() and "555" not in pii_log
