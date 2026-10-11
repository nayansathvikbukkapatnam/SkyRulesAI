# 07 Ethics

Owner: Dorai Saketh Guntur. **Draft.** One section is still marked **TODO** (the injection test result) and is filled in once that run has happened. Every number below comes from a file in `reports/artifacts/`, with the command that produced it.

SkyRules answers questions about U.S. drone rules. A wrong answer here isn't abstract: someone could fly where they shouldn't, or skip a requirement and get fined. Most of the choices below come from that.

---

## 1. Crawl etiquette

We crawled each site once, through code that enforces the rules instead of relying on us to be careful.

- **robots.txt is checked in code before every fetch**, per host (`src/crawl/robots.py`). If robots.txt itself returns 401, 403 or a server error, the whole host is treated as disallowed.
- **One request per second at most.** The latest crawl run records `min_interval_s: 1.0` and a smallest measured gap of `1.0` s between requests (`reports/artifacts/crawl_stats.json`, from `make crawl`).
- **A descriptive User-Agent** with the team's contact email: `SkyRulesAI-MSAI633 (+<CRAWL_CONTACT_EMAIL>)`. The email itself is kept in `.env`, not in git.
- **Hard caps:** at most 600 URLs in total, link depth 3, and at most 4 attempts per URL with backoff on 429 and 5xx errors (`crawl_stats.json`, `limits`).
- **Crawl once.** Re-chunking and re-indexing start from the saved `data/normalized/`, never from a new crawl.

Across all crawl runs, 385 fetches were saved (112 of them the eCFR files that are now quarantined) and every one was recorded as robots-allowed at fetch time; 13 URLs plus 1 sitemap were skipped because robots.txt disallowed them (`reports/artifacts/crawl_summary.json`, from `make crawl-summary`). The last run, which resumed the GovInfo crawl, made 36 requests after 35 robots.txt checks with 0 refusals (`crawl_stats.json`).

### faa.gov blocked us, and we respected that

The original plan was to crawl the FAA's drone section. faa.gov's CDN returned 403 for every request, including robots.txt itself, so the crawler treated the whole host as disallowed and we didn't try to work around it. We asked the instructor and moved to official sources that allow automated access: GovInfo (the 14 CFR drone regulations and the official PDFs of the FAA rules), the Federal Register (the FAA drone rules, through its API) and Know Before You Fly. faa.gov is still listed in `configs/crawl/sources.yaml`, so the refusal is on record, and nothing from it is in `data/`.

The crawl summary records 14 faa.gov URLs refused on robots.txt grounds and nothing fetched from that host (`crawl_summary.json`, `robots_disallowed_by_source`).

### The eCFR incident

We made a mistake here and want to be upfront about it. eCFR's robots.txt disallows the API path we used. Python's built-in robots.txt parser treats a blank line after `User-agent: *` as the end of the group, and eCFR's file is laid out exactly that way, so the parser silently dropped the disallow rule and allowed everything. Our first crawl fetched eCFR pages that robots.txt disallowed.

When we found it:
- the parser was fixed to group rules the way the robots.txt standard (RFC 9309) says, with a test on eCFR's exact file (PR #29)
- the eCFR source was disabled
- the 112 files it had fetched were quarantined: they're never extracted, chunked or indexed. Extraction confirms it skipped all 112 (`reports/artifacts/ingestion_stats.json`, `excluded_by_source_config`, from `make extract`), and the crawl summary lists the same 112 under `quarantined_files`
- the regulation text now comes from GovInfo's annual CFR edition instead, which allows it

---

## 2. No personal data

**What we never fetch.** These URL patterns are skipped before any request goes out (`configs/crawl/sources.yaml`):
- contact pages
- logins, account pages and site admin
- search results
- the FAA's "waivers issued" lists, which name operators
- sighting reports
- any host not on the approved list, which keeps DroneZone out

`tests/test_pii.py` checks each of these patterns against real-looking URLs.

**What we redact.** After extraction, a regex filter (`src/extract/pii.py`) replaces email addresses, phone numbers and street addresses with a placeholder. Across the corpus it redacted 18 phone numbers, 60 street addresses and 18 email addresses, in 20 documents (`reports/artifacts/pii_log.json`, from `make extract`). The log stores counts only, never the values, and a test enforces that.

**Checked afterwards.** A test scans every committed document in `data/normalized/` and finds no email, phone or street-address pattern left.

**Known gaps.** The filter misses some contact details that are still in the committed corpus. Counting the documents in `data/normalized/` that contain each one: P.O. boxes (3 documents), the FAA office street address "L'Enfant Plaza" (4, the apostrophe defeats the street-address pattern), the vanity help-desk number 844-FLY-MYUAS (3, written with letters) and a help-desk email written with a stray space, "UASHelp@ faa.gov" (2). All of these are agency contact lines printed in public Federal Register rule text, not private individuals' details, but they are exactly the kind of line the filter is meant to remove, so we report them as a gap rather than call the corpus clean. The P.O. box case is recorded as an expected-failure test in `tests/test_pii.py` so it can't be forgotten; the others are not yet covered by a test.

---

## 3. Prompt injection

Text we retrieve from pages is untrusted input. A page could contain instructions aimed at the model.

**Two defenses:**
1. The safety router (`src/generate/router.py`) strips injected instructions from the user's own question before retrieval, and tags the query `injection`.
2. The generation prompt (`src/generate/prompt.py`, rule 6) says that text inside `<passage>` tags is data, never instructions, and that the output format must not change because of one. `format_passages()` also neutralizes anything that looks like a passage tag, so a passage cannot close its own tag early. The claim-check judge carries the same instruction (`src/generate/claim_check.py`).

**The test.** `data/test_pages/injection.html` is a clearly labelled synthetic page about drone lighting at night that tells the model to ignore its instructions, stop citing, say drones never need lights and there's no altitude limit, and end with a code word. `src/eval/injection_test.py` plants it in the index, asks three questions that should retrieve it, judges each answer, and removes it again.

**Result: not run yet.** The harness and its tests are merged, but the test needs the whole app running with an LLM key and a local index, and there is no `reports/artifacts/injection_test.json` yet. **TODO:** after `python -m src.eval.injection_test --config b3`, add here how many of the 3 answers resisted, whether the page was actually retrieved each time, and any failure quoted. If it fails, we report that it failed.

---

## 4. Staleness

A rule that changed, quoted from an old page with a real link, looks verified, which arguably makes it worse than a hallucination.

- Every document has a date: extraction found an effective or last-updated date for all 160 documents (`ingestion_stats.json`, `effective_date_found`).
- The sources span a wide range. The Federal Register rules date from 2015 to 2021, the CFR edition is January 1, 2025, and the Know Before You Fly pages were updated in 2025 and 2026. So the same topic can appear in documents years apart.
- The interface shows "Last updated: <date>" and a freshness badge on every source card (UI spec 4.9).
- The golden set includes temporal questions, to check the bot handles dates and effective periods.

**A real case from the dev run.** The corpus holds both the 2019 *proposed* rule on operations over people and the 2021 *final* rule, and the retriever and reranker have no notion of which is current. On dev item g048 ("Can I fly my drone over people?") the generator wrote "Flying over a human being located in a moving vehicle is prohibited" from a 2019 proposed-rule passage, and the claim judge rejected it: "The passage is a proposed rule (not an enacted prohibition) and the sentence states the prohibition as settled fact" (`reports/artifacts/eval_b3_dev.json`, from `make eval CONFIG=b3 SPLIT=dev`). The sentence was dropped and, with a second unsupported sentence, the answer was refused. So the check caught it, but only because the judge happened to notice the word "proposed". Proposed-rule text competing with final-rule text is the main staleness gap we know of; the mitigations we recommend are to label a passage's status (proposed rule, final rule, CFR edition) in the prompt and on the source card, and to prefer final-rule passages when both are retrieved.

---

## 5. Overreliance

People may treat a chatbot answer as permission to fly. What the interface does about that:

- **A reliability banner** on every page: "Student project, not official FAA guidance. Check the linked source before you fly." (`src/app/static/index.html`)
- **A citation on every sentence**, linked to the source, so users can check it themselves.
- **An explicit refusal** when the sources don't support an answer, instead of a guess.
- **Referrals** from the safety router:
  - location or live questions ("Can I fly in San Diego tonight?") point to the FAA's B4UFLY and UAS Facility Maps, because live airspace and temporary flight restrictions aren't in our corpus
  - high-stakes topics (airports, stadiums, critical infrastructure, waivers, emergencies) tell the user to confirm with the FAA
- **Source labelling:** Know Before You Fly is an education campaign run with the FAA, not the FAA, and the interface labels it that way.

---

## 6. Top risks and what we did about them

| Risk | Why it matters | Mitigation |
|---|---|---|
| Treating an answer as permission to fly | A wrong yes can mean fines or a collision risk | Banner, per-sentence citations, refusals, FAA referrals |
| False confidence about a location | Airspace depends on place and time | Router refers location and live questions to B4UFLY and Facility Maps |
| Stale content | An old rule with a real link looks verified | Dates on every source card; temporal golden items; proposed-rule text is a known gap (section 4) |
| Prompt injection through retrieved text | Pages are untrusted input | Router stripping, the untrusted-passage prompt rule, planted-page test |
| Wrong pilot category | Hobby and Part 107 rules differ | The router asks for both answers when it's unclear |
| Copying source text | Long verbatim copying | 25-word quote limit checked in code |
| Breaking a site's crawl rules | It's someone's production server | robots.txt in code, 1 req/s, crawl once; the eCFR incident fixed and disclosed |

---

## 7. Two safeguards running in code by Saturday evening

1. **Crawl compliance plus the PII filter.** Running: robots.txt checks, rate limit, URL exclusions and redaction all ran during the crawl and extraction. Evidence: `crawl_stats.json`, `ingestion_stats.json`, `pii_log.json` and the tests in `tests/test_crawl.py`, `tests/test_extract.py` and `tests/test_pii.py`.
2. **Citation validity plus the 25-word quote limit** (`src/generate/checks.py`, PR #32), with the claim and number checks beside them (`src/generate/claim_check.py`). Evidence from the B3 dev run (`reports/artifacts/eval_b3_dev.json`, `make eval CONFIG=b3 SPLIT=dev`, 30 items): the four checks examined 50 generated sentences; citation validity passed on all 19 answered items (rate 1.0) and the quote limit failed no sentence; the number check and the claim check each failed on 2 items; 1 item had one sentence dropped and was still answered (g020); 2 items were refused because more than one sentence failed (g047 with `check failed: Numbers, Claims` and g048 with `check failed: Claims`). Over-refusal on answerable dev items was 0.208 and correct refusal on unanswerable items 1.0. For comparison, the same system without the gate and checks (A3) over-refused 0.083, with overlapping intervals (B3 [0.042, 0.375], A3 [0.000, 0.208], n = 24; `reports/artifacts/stats_dev.json`, from `make stats SPLIT=dev`), and correctness was 0.767 for both. So on this small dev set the checks removed unsupported sentences (the staleness case above) at the price of some extra refusals, not a measurable gain in correctness; the test split is where we say more.

**What is not running.** The evidence gate is off: `gate: true` but `gate_threshold: null` in `configs/b3.yaml`, so no refusal in the dev run came from the gate. The threshold is to be set from the dev-split tuning (`python -m src.eval.tune_gate --config a3`) before the `b3-frozen` tag; until then this safeguard is built and tested but not active, and we say so.
