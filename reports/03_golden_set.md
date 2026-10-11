# 03. Golden evaluation set

Owner: Dorai Saketh Guntur. Every number below comes from `reports/artifacts/golden_check.json` (written by `make golden-check`) or `eval/split.json` (written by `make split`), and the file is named next to it. Nothing in this report is typed from memory.

## 1. What the set is for

The golden set is the yardstick for every number in reports 04 to 06. It is 50 questions a real drone flyer might ask, each with a human-written answer taken from the source page, the page's URL and a short evidence quote, so that a wrong answer, a missing citation or a refusal can be scored against something a person checked. Ten of the questions have no answer in the corpus on purpose, to measure refusals.

## 2. Counts against the targets

`make golden-check` validates the file (schema, quotas, verbatim quotes, verifier not the author) and writes `reports/artifacts/golden_check.json`. The latest run reports `ok: true`, 50 items and 0 items with errors.

| Category | Spec minimum | Our target | In the set |
|---|---|---|---|
| single_hop | 15 | 18 | 18 |
| multi_hop | 6 | 7 | 7 |
| comparative | 4 | 5 | 5 |
| temporal | 4 | 5 | 5 |
| unanswerable | 8 | 10 | 10 |
| ambiguous | 3 | 5 | 5 |
| **Total** | **40** | **50** | **50** |

Source: `golden_check.json`, `quotas`.

## 3. How and when the items were written

- **Who.** Dorai wrote all 50 items (`golden_check.json`, `items_by_author`: Dorai 50). No item was drafted by an AI tool; Claude Code was used only to run the checker, to compare quotes against `data/normalized/`, and to build the verification tooling (team rule 4 in `CLAUDE.md`).
- **From what.** Each item was written with the source page open in a browser. The sources are the approved corpus sources only: the GovInfo 2025 edition of 14 CFR (one XML document per section of Parts 107, 89 and 48), the FAA drone rules in the Federal Register (full text), their official GovInfo PDFs, and Know Before You Fly guidance pages. `source_url` is the exact URL that is in the corpus, so the checker can confirm every quote appears verbatim in the saved document (`quotes_checked: true`).
- **When.** The first ten items (issue #16, Friday) cited faa.gov pages. faa.gov blocks automated access, the sources changed on Saturday October 10, and those items were re-sourced to the approved sources. The full 50-item set was written on October 10 and merged in PR #56 at 22:10 UTC. The chatbot had become runnable the same evening (`answer()` merged at 21:30 UTC in PR #52, the chat page at 22:05 UTC in PR #55), but no evaluation run existed yet: the first dev-split runs were merged on October 11 at 00:47 UTC (PR #74). No question, gold answer or quote was changed after the first run; the verification PRs changed only `verified_by` and `verified_date`.
- **Fields.** Every answerable item has `gold_answer`, `source_url` and an `evidence_quote` of 25 words or fewer copied from the page. Sixteen items that combine pages carry a list of URLs with a quote per URL, in the same order. Every unanswerable item has `searched_for`: the exact terms searched in `data/normalized/` (160 documents) and the number of hits, so the absence is on record. The field rules are in `eval/README.md`.

## 4. Verification

Dorai verifies none of the items, because Dorai wrote them all. `eval/verification_assignments.json` assigns contiguous blocks to the other members so that merge conflicts stay at block boundaries. Each verifier opened the source page, confirmed the gold answer and that the quote appears word for word (or, for unanswerable items, repeated the search), then set `verified_by` and `verified_date` on their own lines in a small PR from their own account, reviewed by Dorai. Verifiers were told not to edit a question, answer or quote, and to report problems instead.

| Verifier | Items | PR |
|---|---|---|
| Pavan | g001 to g013 (13) | #67 |
| Nayan | g014 to g026 (13) | #66 |
| Sai | g027 to g050 (24) | #69 |

Source: `golden_check.json`, `items_by_verifier` (Nayan 13, Pavan 13, Sai 24). Adinath's block (g039 to g050) was taken over by Sai so the set could be verified before the split; the assignment file still lists the original four-way plan. All 50 items were verified on 2026-10-10, and the checker confirms that no item is verified by its author.

## 5. The dev/test split

`make split` (`src/eval/split.py`) made the split once, after verification, with scikit-learn's `train_test_split`, `test_size=0.4`, stratified by category, seed 42. It refuses to overwrite `eval/split.json`, and the file records the SHA-256 of `golden.jsonl` at the time (`golden_sha256`), so a later edit to the set would be detectable.

| Category | Dev | Test |
|---|---|---|
| single_hop | 11 | 7 |
| multi_hop | 4 | 3 |
| comparative | 3 | 2 |
| temporal | 3 | 2 |
| unanswerable | 6 | 4 |
| ambiguous | 3 | 2 |
| **Total** | **30** | **20** |

Source: `eval/split.json`, `counts`.

All tuning (the gate threshold, prompt changes, retrieval settings) uses the dev split only. `make eval SPLIT=test` and `make retrieval-eval SPLIT=test` refuse to run unless `FINAL=1` is given and the `b3-frozen` git tag exists (`src/eval/retrieval_metrics.py`, `split_ids`). The test split is run once, on Sunday, and report 06 states the date and commit of that run.

## 6. Example items

One per category, quoted from `eval/golden.jsonl`:

| Id | Category | Question | Gold answer or search record |
|---|---|---|---|
| g001 | single_hop | What is the maximum altitude for a small drone flown under Part 107? | No higher than 400 feet above ground level (with a limited exception near structures). |
| g019 | multi_hop | I'm 17 and want to start paid drone work with a 45-pound drone. Am I old enough for a remote pilot certificate, and is my drone small enough for Part 107? | Yes on both. The minimum age is 16, so 17 qualifies, and Part 107 covers small unmanned aircraft weighing less than 55 pounds. |
| g026 | comparative | How do recreational flyers and commercial drone pilots each show they are qualified to fly? | Recreational flyers take TRUST and carry proof of passing. Commercial pilots obtain a remote pilot certificate from the FAA. |
| g031 | temporal | When did the Remote ID final rule take effect? | March 16, 2021. |
| g036 | unanswerable | Which drone should I buy for aerial photography? | Case-insensitive search of `data/normalized` for "best drone" and "which drone should": 0 hits; "recommend ... drone" has 4 hits, none a product recommendation. |
| g046 | ambiguous | Can I fly here? | Ask where and what kind of flying, or give the general rules for both and refer the user to the FAA's location tools. Do not guess. |

## 7. How unanswerable items were confirmed absent

For each of the 10 unanswerable items, the `searched_for` field records a case-insensitive search of every document in `data/normalized/` for the terms a user would use, the hit count, and why any hits do not answer the question (for example "recommend" appearing in the name of an advisory committee). The verifier repeated the search. The questions are about things an official rules corpus does not contain: product and camera recommendations, prices of specific drones, insurance providers, and similar.

## 8. Limitations

- 50 items, and 20 in the test split, is small. Every rate in reports 04 to 06 is reported with a 95% bootstrap interval or a McNemar test, and differences of a few items are noise.
- All items were written by one person on one day, so they share one person's phrasing habits. The ambiguous and temporal categories are the hardest for the system (the A3 dev retrieval run in `reports/artifacts/retrieval_a3_dev.json` shows zero evidence recall on both), which is useful, but with 5 items each those cells are very wide.
- The items were written after the chatbot code existed, though before any run or tuning. The spec asks for Friday authoring; the source change on Saturday morning forced the rewrite.
