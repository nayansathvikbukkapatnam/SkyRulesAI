# SkyRules AI: final deliverables checklist

This is the definition of "finished" for the residency project, written in the team's own words from the assignment requirements. Claude Code and every teammate use it to check that the final version is complete.

- **Due:** Sunday, with a live presentation of 8 to 10 minutes.
- **Graded on:** the GitHub repo, the working chatbot, the Markdown reports in `reports/` and the live demo.
- **Owners and timing:** see `CLAUDE.md` section 9.

## Grading weights (where effort goes)

| Component | Weight | What earns it |
|---|---|---|
| Chatbot and pipeline | 20% | All nine stages working end to end, 150+ pages with PDFs and tables, grounded cited answers shown live |
| Retrieval and reranking | 15% | Dense, BM25 and hybrid all measured; metadata filtering works; reranker lift, latency and pool ceiling reported |
| Golden evaluation set | 15% | 40+ items, category minimums met, written before tuning, gold URLs and quotes recorded, human-verified, stratified split |
| Evaluation rigor | 15% | Four baselines, B0 leakage reported, one-change-at-a-time ablation, confidence intervals or paired tests, 15+ labelled failures |
| Chunking and re-chunk | 10% | Two strategies compared with real metrics, migration without re-crawl, deterministic IDs, no orphaned chunks |
| Version control and teamwork | 10% | Protected main, reviewed PRs, every member's commits across 2+ stages, clear messages, task board, no secrets |
| Grounding, refusal and ethics | 10% | Valid citations, both refusal rates reported, quote limit enforced, high-stakes referral, safeguards demonstrated |
| Reports and reproducibility | 5% | All eight reports with real numbers, pinned dependencies, documented commands that reproduce every number |

A narrow chatbot with honest measurement beats a broad one without it. An honest low number beats an unsupported high one, and reporting a number the code did not compute is treated as falsification.

---

## 1. Pipeline (all nine stages)

- [ ] **Scrape:**
  - [ ] Crawls only allowed `www.faa.gov` drone paths.
  - [ ] robots.txt is checked in code, at most 1 request per second, with a User-Agent containing the team email.
  - [ ] Raw bytes are saved with URL, timestamp and SHA-256 before parsing.
  - [ ] 150+ pages, including 5+ PDFs and 3+ pages with data tables.
  - [ ] No login pages or directories.
- [ ] **Extraction:**
  - [ ] Navigation, footers and banners are stripped; PDF text is extracted; tables stay tables; duplicates are removed by hash.
  - [ ] Each document stores `doc_id`, `source_url`, `title`, heading path, `page_type`, `effective_date` (when found) and crawl timestamp.
- [ ] **Chunking:** fixed-size with overlap AND structure-aware (heading path carried into each chunk). Chunk counts and token-length distributions are reported for both.
- [ ] **Embedding:** the model name and version, dimension, distance metric and L2 normalization are recorded, with a test proving the metric matches the normalization.
- [ ] **Vector database:** deterministic chunk IDs, metadata filtering on `page_type`, and a working delete path.
- [ ] **Re-chunk:**
  - [ ] Re-chunked and re-indexed from saved normalized text, with no re-crawl.
  - [ ] Stale chunks from the old strategy are deleted, with proof that no orphans remain.
  - [ ] The same evaluation is re-run and the change reported.
- [ ] **Retrieval:** dense, BM25 and hybrid fusion are all implemented and measured, with a candidate pool of 20 or more.
- [ ] **Reranking:** the pool is reranked to the top 5 or fewer with a cross-encoder. Metric lift, added latency and Recall@20 of the pool (the reranker's ceiling) are reported.
- [ ] **Generation:**
  - [ ] Answers only from retrieved context, with a source URL cited for every factual claim.
  - [ ] No more than 25 consecutive words quoted from any page.
  - [ ] Refuses when context is insufficient.

## 2. Chatbot behavior

- [ ] Chat interface with conversation history, so follow-up questions work
- [ ] Clickable source citations on every factual answer
- [ ] Visible refusal: an explicit "I don't have that information" instead of a guess
- [ ] Expandable retrieval trace showing retrieved chunks with scores before and after reranking
- [ ] Referral to a qualified human or official tool for high-stakes questions (the FAA, B4UFLY, UAS Facility Maps)
- [ ] An interface message about its own reliability (not official FAA guidance)
- [ ] Content age shown on sources (last updated date)

## 3. Golden evaluation set

- [ ] 40+ items (target 50) in `eval/golden.jsonl`
- [ ] Category minimums (and our targets):

| Category | Minimum | Our target |
|---|---|---|
| Single-hop factual | 15 | 18 |
| Multi-hop (2+ chunks from 2+ pages) | 6 | 7 |
| Comparative | 4 | 5 |
| Temporal | 4 | 5 |
| Unanswerable | 8 | 10 |
| Ambiguous or adversarial | 3 | 5 |

- [ ] Written from the FAA pages BEFORE the chatbot worked, by team members, not by AI
- [ ] Every answerable item has a gold answer, source URL and an evidence quote of 25 words or fewer; every unanswerable item records what was searched
- [ ] Every gold answer verified against the page by a human other than the author
- [ ] Stratified 60/40 dev/test split, seed recorded; tuning on dev only; test run exactly once at the end, and the report says so

## 4. Evaluation

- [ ] **Baselines** (all four reported):
  - B0: retrieval disabled (closed book)
  - B1: BM25 only
  - B2: dense with fixed chunks
  - B3: final system
- [ ] **B0 leakage:** the headline metric is also reported on the subset B0 got wrong
- [ ] **Metrics:**
  - Recall@5, Recall@20, MRR, nDCG@10
  - answer correctness
  - citation validity
  - correct-refusal rate (unanswerable items) and over-refusal rate (answerable items)
  - p50 and p95 latency
  - cost per query
- [ ] **Ablation:** one change at a time from B2 to B3 (structure chunks, then hybrid, then reranker, then checks)
- [ ] **Uncertainty:** every improvement claim has a 95% bootstrap interval or a McNemar test on per-item pass/fail
- [ ] **Error analysis:** 15+ failures hand-labelled as one of these:
  - retrieval miss, rank failure, chunk boundary, extraction failure, stale content
  - reranker demotion, generator ignored context, invalid citation
  - over-refusal, hallucination

## 5. Reports (`reports/`, Markdown, real numbers only)

| File | Owner | Must contain |
|---|---|---|
| `00_proposal.md` | Team | The proposal as submitted (done) |
| `01_ingestion.md` | Adinath | Crawl policy and evidence (robots checks, rate limit, User-Agent); counts of pages, PDFs, table pages, duplicates removed, skipped URLs by reason; extraction method per type; extraction failures; how often `effective_date` was found; PII filter results (safeguard 1) |
| `02_chunking.md` | Nayan + Sai | Both strategies; chunk counts; token-length table and histograms; chunk ID scheme; embedding record (model, version, dimension, metric, normalization); migration log with before/after counts and orphan check; dev metrics change from fixed to structure-aware |
| `03_golden_set.md` | Dorai | Category counts against targets; how and when items were written (before the system worked); verification process and who verified; split method and counts; example items; how unanswerables were confirmed absent |
| `04_retrieval_reranking.md` | Sai | Dense vs BM25 vs hybrid metrics with intervals; metadata filter demonstration; reranker lift and added latency; Recall@20 pool ceiling |
| `05_generation.md` | Pavan | Prompt and grounding design; citation validity; correct-refusal and over-refusal rates; quote-limit enforcement; referral behavior; how answer correctness is judged and how the judge was checked against humans; latency p50 and p95; cost per query |
| `06_final_performance.md` | Adinath | B0 to B3 on the test split (run once; state the date and commit hash); B0-incorrect subset; ablation table with intervals or McNemar; 15+ labelled failures with counts per category; known limitations |
| `07_ethics.md` | Dorai | Crawl etiquette; PII exclusion rules and evidence; prompt injection test and result; how content age is shown (staleness); what the interface says about its own reliability (overreliance); top ethical risks and mitigations; evidence that two safeguards ran in code by Saturday evening |

Every table is generated from `reports/artifacts/` and names the command that produced it. A placeholder counts as missing.

## 6. README

- [ ] Project summary and team with roles
- [ ] Setup steps from a fresh clone (`make setup`, `.env` keys, AWS profile)
- [ ] The exact commands that reproduce every reported number
- [ ] Known limitations
- [ ] AI usage: which tools were used, how, and on which files, at each milestone

## 7. Version control and teamwork

- [ ] `main` protected, all work through short-lived feature branches and pull requests
- [ ] Every PR reviewed and approved by a teammate before merging
- [ ] Every member has commits under their own account touching 2+ pipeline stages
- [ ] Short present-tense commit messages describing what and why (no "update" or "wip")
- [ ] Project board or issues showing the owner of each stage and report, kept current
- [ ] `.env` and credentials git-ignored from the start; no key ever committed
- [ ] Merge conflicts resolved deliberately and explained in the PR

## 8. Ethics and conduct

- [ ] Crawl etiquette enforced in code (robots.txt, rate limit, User-Agent, no login or directory pages)
- [ ] No PII: directory and contact pages skipped; personal contact details dropped or redacted
- [ ] Prompt injection: a planted page with "ignore your previous instructions" tested, result reported
- [ ] Staleness: content age shown to users and discussed
- [ ] Overreliance: the interface states its limits and refers high-stakes questions
- [ ] Two safeguards running in code by Saturday evening, with evidence in `07_ethics.md`:
  1. Crawl compliance plus the PII filter
  2. Citation validity plus the 25-word quote limit

## 9. Live demo (8 to 10 minutes, Sunday)

- [ ] A normal cited answer, with the citation preview and the retrieval trace opened
- [ ] At least one refusal case (for example "Which drone should I buy?")
- [ ] The location referral (the San Diego night-flight question)
- [ ] At least one before/after comparison (B2 vs B3 on the same question)
- [ ] The headline results table from `06_final_performance.md`
- [ ] Every member able to explain any stage, metric formula or number during Q&A

## 10. Final submission check (Sunday, before presenting)

- [ ] All boxes above ticked, or the gap stated honestly in the relevant report
- [ ] Fresh clone, then `make setup` and the README commands work
- [ ] `b3-frozen` tag exists, and the test split ran once after it
- [ ] No secrets in git history (`git log -p | grep -i -E "aws_secret|access_key"` returns nothing)
- [ ] All PRs merged, board up to date
