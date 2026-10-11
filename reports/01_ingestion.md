# 01 Ingestion: crawl and extraction

**Owner:** Adinath (crawl, extraction, PII filter).

Every number in this report is generated from `reports/artifacts/` by `make report-ingestion`, which fills the marked tables. Nothing between the `AUTO` markers is typed by hand. The prose below gives no counts of its own; where it refers to a quantity, the number is in the table next to it.

**Reproduce, in order** (the crawl needs `CRAWL_CONTACT_EMAIL` in `.env`; on Windows run the modules with `.venv/Scripts/python -m ...`):

```bash
make crawl                        # python -m src.crawl.crawler: data/raw/ + crawl_stats.json
make crawl-summary                # python -m src.crawl.summary: crawl_summary.json (all runs)
make extract                      # python -m src.extract.normalize: data/normalized/ + ingestion_stats.json + pii_log.json
make report-ingestion             # fills the tables below
```

`data/raw/` is not committed (it is gitignored), so the crawl artifacts are the evidence of what was fetched; `data/normalized/` is committed and is the source of truth for every later stage.

## 1. What we crawled, and why it is not only faa.gov

The proposal planned to crawl `www.faa.gov/uas`. The corpus ended up coming from other official sources about the same U.S. drone rules. This is what happened, in order.

1. **faa.gov refused our crawler.** The first request of the first run, for `https://www.faa.gov/robots.txt`, returned **HTTP 403 Access Denied** from the site's CDN (Akamai), for our honest User-Agent. Our robots check treats an unreadable robots.txt as "disallow everything", so the crawler stopped without fetching a single page. The same client and network got normal responses from other .gov sites, so the block was specific to faa.gov. We did **not** try to get around it (no browser User-Agent, no header tuning): that would have defeated the site's bot protection and broken our own crawl rules. faa.gov stays listed in `configs/crawl/sources.yaml`, so every run records the refusal (section 3, `robots_disallowed`).
2. **We moved to official sources on the same rules** whose robots.txt allows crawling: the Federal Register (the FAA's drone rules as published), govinfo (the official PDFs and the annual Code of Federal Regulations), and Know Before You Fly (plain-language guidance from an education campaign run in partnership with the FAA; it is not the FAA, and the bot should present it that way).
3. **eCFR and the Federal Register only allow automated access through their APIs.** Their web pages redirect automated requests to a "Request Access" page that says programmatic access is limited to their developer APIs. The crawler refuses a redirect to another host, so nothing was fetched from that page, and we switched to the documented API routes.
4. **We found and fixed a robots.txt bug in our own checker, and quarantined what it let through.** eCFR's robots.txt has a blank line after `User-agent: *`. Python's `urllib.robotparser` ends the group at that blank line and drops every rule after it, so our checker treated eCFR as "allow everything", and the crawl fetched eCFR's renderer API, which that file disallows (`Disallow: /api/renderer/v1/content/`). We found this while checking the API paths against eCFR's robots.txt by hand. The fix (`src/crawl/robots.py`) parses robots.txt without blank lines, which matches the grouping rules of RFC 9309, and a test uses eCFR's exact file. eCFR is disabled in `sources.yaml`, its files are moved to `data/raw/quarantine/` and are never extracted, and every other URL in the manifest was re-checked against freshly downloaded robots.txt files with the fixed parser: all allowed. The counts are in section 3 ("files from sources now disabled", "quarantined raw files") and section 4 ("excluded by `sources.yaml`").
5. **The regulation text now comes from govinfo's annual CFR edition** (14 CFR revised as of January 1, 2025; the 2026 edition was not yet published at crawl time), Parts 107 (small UAS), 89 (Remote ID) and 48 (registration). govinfo's robots.txt allows `/content/pkg/`. We first used the per-section PDFs, but each one prints whole pages, and in some of them the section's own text is missing from the text layer, which would have produced documents titled with one section and holding another. We switched to govinfo's per-section XML, which has the section number, caption and paragraphs as separate elements. The earlier section PDFs stay in the manifest and are excluded from the corpus.

Current 14 CFR Part 101 has no Subpart E, so the old model-aircraft rule is not in the CFR (recreational flying is now governed by the statute 49 U.S.C. 44809). Recreational rules in the corpus come from the Federal Register and Know Before You Fly.

**Approval.** The instructor approved moving the corpus to these official sources on October 10, 2026, after the faa.gov block (recorded in `CLAUDE.md`, "Current status and plan", item 1). The crawl rules did not change: robots.txt before every request, 1 request per second, the honest User-Agent and the PII exclusions.

## 2. Crawl policy, enforced in code

All of this is in `src/crawl/` and tested in `tests/test_crawl.py` against a fake site and a fake clock (no network in tests).

| Rule (CLAUDE.md rule 1) | How the code enforces it |
|---|---|
| Only allowed hosts and paths | `configs/crawl/sources.yaml` lists every host with `crawl` (fetch and follow links), `leaf` (fetch only) and `exclude` patterns; `src/crawl/sources.py` decides crawl, leaf or skip. Any other host is never requested, including after a redirect, and each redirect hop is checked. |
| robots.txt before every request | `src/crawl/robots.py` reads robots.txt once per site and checks every URL and every redirect hop before the request. It honours `*` and `$` wildcards with longest-match precedence (`urllib.robotparser` escapes them, so wildcard rules never matched) and RFC 9309 grouping (blank lines do not end a group). An unreadable robots.txt (5xx, 401, 403, network error) blocks the whole site. |
| At most 1 request per second | `src/crawl/ratelimit.py` keeps at least 1.0 s between request starts across all sites, raised to a site's `Crawl-delay` if it is longer, and records the smallest gap it measured. |
| Descriptive User-Agent with team email | `SkyRulesAI-MSAI633 (+<CRAWL_CONTACT_EMAIL>)`; the address is read from `.env` and never written to an artifact. |
| Back off on 429 and 5xx | `tenacity`, exponential wait, limited attempts per URL. |
| Raw bytes saved before parsing | `data/raw/{sha256}.bin` is written first, then one line in `data/raw/manifest.jsonl` with URL, final URL, timestamp, status, content type, SHA-256 and path. A test checks the order. |
| Crawl once | The crawler refuses to run when the manifest already has 150 or more entries unless `--force` is given, and never re-fetches a URL already in the manifest. `--force` was used only to add the govinfo CFR source. |
| No PII pages (rule 2) | Contact pages, login and sign-in pages, account areas, search, site admin and feeds are excluded on every site; faa.gov also excludes waiver lists, sighting reports and the Spanish section. Excluded URLs are never requested. |

<!-- BEGIN AUTO:crawl_settings -->
| Setting | Value |
|---|---|
| User-Agent | `SkyRulesAI-MSAI633 (+<CRAWL_CONTACT_EMAIL>)` (address from `.env`) |
| Minimum gap between requests (s) | 1 |
| Attempts per URL on 429 / 5xx (exponential backoff) | 4 |
| Link depth from a seed | 3 |
| URL cap, all sources | 600 |
| URL cap, `faa` (www.faa.gov) | 400 |
| URL cap, `ecfr` (www.ecfr.gov), disabled | 200 |
| URL cap, `federalregister` (www.federalregister.gov) | 20 |
| URL cap, `govinfo` (www.govinfo.gov) | 300 |
| URL cap, `knowbeforeyoufly` (knowbeforeyoufly.org) | 100 |
| URL cap, `wayback` (web.archive.org), disabled | 400 |

Sources: `reports/artifacts/crawl_stats.json` (latest run) and the per-source caps in `configs/crawl/sources.yaml`.
<!-- END AUTO:crawl_settings -->

## 3. Crawl results

### Saved fetches by source (all runs)

<!-- BEGIN AUTO:raw_store -->
| Source | application/pdf | application/xml | text/html | Saved fetches |
|---|---|---|---|---|
| `ecfr` | 0 | 0 | 112 | 112 |
| `federalregister` | 0 | 0 | 8 | 8 |
| `govinfo` | 120 | 112 | 0 | 232 |
| `knowbeforeyoufly` | 11 | 0 | 22 | 33 |
| **Total** | 131 | 112 | 142 | 385 |

Distinct SHA-256 among saved files: 385. First and last fetch (UTC): 2026-10-10T18:53:12+00:00 and 2026-10-10T20:24:40+00:00. Files from sources now disabled: `ecfr`: 112; quarantined raw files: 112. Source: `reports/artifacts/crawl_summary.json`.
<!-- END AUTO:raw_store -->

### Skipped URLs by reason (all runs)

`max_urls_for_source` is mostly from the dry runs, which cap every source at a few URLs. `other_host` is links to social media, partner sites and other domains found on crawled pages.

<!-- BEGIN AUTO:skipped -->
| Reason (never requested, or not saved) | Distinct URLs |
|---|---|
| `duplicate_of_fetched_url` | 1 |
| `excluded_login` | 22 |
| `index_http_404` | 1 |
| `max_urls_for_source` | 163 |
| `other_host` | 435 |
| `out_of_scope` | 7 |
| `robots_disallowed` | 13 |
| `sitemap_robots_disallowed` | 1 |
| `unsupported_file_type` | 2 |
| **Total distinct URLs** | 645 |

URLs refused by robots.txt, by source: `faa`: 14. A URL skipped in several runs is counted once (908 log lines in all). Source: `reports/artifacts/crawl_summary.json`.
<!-- END AUTO:skipped -->

### Latest run

Each run rewrites `crawl_stats.json`; the copies written by earlier runs are in the git history of that file.

<!-- BEGIN AUTO:latest_run -->
| Latest run | Value |
|---|---|
| Started / finished (UTC) | 2026-10-10T20:24:04+00:00 / 2026-10-10T20:24:40+00:00 |
| HTTP requests | 36 |
| Smallest measured gap between requests (s) | 1 |
| robots.txt checks | 35 |
| URLs refused by robots.txt | 0 |
| Documents saved | 32 |
| Already in the manifest, reused (not re-fetched) | 88 |

Source: `reports/artifacts/crawl_stats.json`.
<!-- END AUTO:latest_run -->

## 4. Extraction

`src/extract/normalize.py` reads the manifest, skips files that `sources.yaml` no longer covers, and writes one document per file to `data/normalized/{doc_id}.json` in the contract of `src/common/contracts.py`. Exact duplicates are dropped by raw SHA-256 and by a hash of the extracted content.

| Source and type | Method | Why |
|---|---|---|
| Know Before You Fly pages | `trafilatura` (Markdown output, headings and tables kept), with a BeautifulSoup fallback when it returns too little text | Ordinary web pages; trafilatura removes navigation and footers and keeps the heading levels. |
| Federal Register full text (HTML) | BeautifulSoup structure walker (`bs4-federalregister`) | On these files trafilatura lost every heading and took "AGENCY:" as the title. The walker keeps the full heading path and every table with its caption, and removes page-number markers and help boxes. |
| govinfo CFR sections (XML) | XML parser (`xml-cfr`, `src/extract/cfr.py`) | Section number, caption, paragraphs and source note are separate elements; `[Reserved]` sections are skipped. |
| PDFs (Federal Register rules, Know Before You Fly handouts) | `pdfplumber`, column-aware (`src/extract/pdf.py`) | Federal Register PDFs are set in three columns: each page is split at its column gutters and read column by column, sideways margin text and running heads are dropped, tables become Markdown, and headings come from font size or short bold lines. |
| PDF pages with no text layer (Know Before You Fly handouts that are pictures of text) | Tesseract OCR (`src/extract/ocr.py`, method `pdfplumber+tesseract`) | Each such page is rendered at 300 dpi and read with Tesseract; only words with confidence of 60 or more are kept, so poster noise does not become corpus text. The document is marked `extraction.ocr_used: true`. A page where OCR fails or finds no confident text is logged, not guessed. |

<!-- BEGIN AUTO:methods -->
| Extraction method | Documents |
|---|---|
| `bs4-federalregister` | 8 |
| `pdfplumber` | 14 |
| `trafilatura` | 22 |
| `pdfplumber+tesseract` | 5 |
| `xml-cfr` | 111 |

| OCR (pages with no text layer) | Value |
|---|---|
| OCR engine | 5.4.0.20240606 |
| Documents with OCR text | 5 |
| Pages whose text came from OCR | 22 |
| Pages where OCR failed or found no confident text | 2 |
| Pages still without text (OCR not available) | 0 |

Source: `reports/artifacts/ingestion_stats.json`.
<!-- END AUTO:methods -->

<!-- BEGIN AUTO:corpus -->
| Corpus | Count |
|---|---|
| Normalized documents written | 160 |
| &nbsp;&nbsp;by source: `federalregister.gov` | 8 |
| &nbsp;&nbsp;by source: `govinfo.gov` | 119 |
| &nbsp;&nbsp;by source: `knowbeforeyoufly.org` | 33 |
| &nbsp;&nbsp;by type: `text/html` | 30 |
| &nbsp;&nbsp;by type: `application/pdf` | 19 |
| &nbsp;&nbsp;by type: `application/xml` | 111 |
| &nbsp;&nbsp;by page_type: `regulation_summary` | 127 |
| &nbsp;&nbsp;by page_type: `guide_page` | 22 |
| &nbsp;&nbsp;by page_type: `toolkit_pdf` | 11 |
| &nbsp;&nbsp;by audience: `general` | 87 |
| &nbsp;&nbsp;by audience: `part107` | 68 |
| &nbsp;&nbsp;by audience: `recreational` | 3 |
| &nbsp;&nbsp;by audience: `public_safety` | 2 |
| Sections | 1871 |
| Tables kept as Markdown | 157 |
| Documents with at least one table | 17 |
| Duplicates removed (same bytes or same extracted content) | 0 |
| Extraction failures | 0 |
| Reserved CFR sections skipped (no text) | 1 |
| Excluded by `sources.yaml`: www.ecfr.gov (source disabled) | 112 |
| Excluded by `sources.yaml`: www.govinfo.gov (no longer in scope) | 112 |

Source: `reports/artifacts/ingestion_stats.json`.
<!-- END AUTO:corpus -->

**Citable URLs.** A Federal Register document's `source_url` is its short public link (`federalregister.gov/d/<number>`), not the API route it was read from; a CFR section's is its govinfo XML; other documents keep the URL they were fetched from.

## 5. Effective dates

| Source | Rule for `effective_date` |
|---|---|
| Federal Register (HTML and PDF) | The date the rule states in its DATES section ("This rule is effective ..."); for a proposed rule, which has none, its publication date. |
| govinfo CFR sections | The edition date (the text in force on January 1, 2025). |
| Know Before You Fly pages | The page's modified date from its metadata (trafilatura). |
| Know Before You Fly PDFs | The PDF's modification date. |

<!-- BEGIN AUTO:dates -->
| Source | Documents | effective_date found |
|---|---|---|
| `federalregister.gov` | 8 | 8 |
| `govinfo.gov` | 119 | 119 |
| `knowbeforeyoufly.org` | 33 | 33 |
| **Total** | 160 | 160 |

Source: `reports/artifacts/ingestion_stats.json`.
<!-- END AUTO:dates -->

## 6. PII filter (ethics safeguard 1)

Two layers, both in code and tested in `tests/test_extract.py`:

1. **Pages that hold personal data are never fetched** (section 2).
2. **Personal contact details in fetched pages are removed** (`src/extract/pii.py`). Emails, phone numbers and street addresses are replaced with `[REDACTED EMAIL]`, `[REDACTED PHONE]` and `[REDACTED STREET_ADDRESS]`, with tests that regulatory numbers (CFR citations, docket and Federal Register numbers) are left alone. The Federal Register "FOR FURTHER INFORMATION CONTACT" block, which names an FAA employee, is dropped whole, in both the HTML and the PDF version. The log keeps counts only.

<!-- BEGIN AUTO:pii -->
| Redacted item | Count |
|---|---|
| `phone` | 18 |
| `street_address` | 60 |
| `email` | 18 |
| **Documents with at least one redaction** | 20 |

Counts only; the matched values are never logged. Source: `reports/artifacts/pii_log.json`.
<!-- END AUTO:pii -->

On inspection, the redacted street addresses are organizational mailing addresses (DOT and FAA headquarters, a docket office, a standards body), not personal ones. Redacting them is over-cautious but harmless.

## 7. Known limitations

- **No faa.gov pages.** The FAA's own plain-language guidance (for example its B4UFLY and recreational-flyer pages) is not in the corpus, because faa.gov refused our crawler. The regulations, the published rules and Know Before You Fly cover the same rules, but the bot cannot cite faa.gov.
- **Know Before You Fly is not the FAA.** It is an industry-led education campaign run in partnership with the FAA. Its pages are guidance, not regulation, and are labelled `guide_page` or `toolkit_pdf`.
- **The CFR text is the January 1, 2025 edition**, not today's eCFR. Any amendment published after that date is missing.
- **OCR text is imperfect.** Pages read by OCR (section 4) are posters and classroom handouts, not rules; their text can contain layout noise, and a few pages had no text Tesseract was confident about. Running OCR needs the Tesseract program installed; without it (as in CI) those pages are reported as needing OCR instead.
- **Near-duplicate content.** Each Federal Register rule is in the corpus twice, as HTML and as its official PDF. They are not exact duplicates (different extraction), so both are kept; retrieval may return both.
- **eCFR files fetched against its robots.txt are kept in quarantine** as evidence for this report and `07_ethics.md`, outside the corpus. They should be deleted after grading.
- **`crawl_stats.json` describes only the latest run.** Totals across runs come from `crawl_summary.json`, which is computed from the crawl logs in `data/raw/` (not committed).
