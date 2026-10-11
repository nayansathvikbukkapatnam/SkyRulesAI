# 06 Final performance

**Owner:** Adinath (statistics, failure analysis).

Every number in this report is generated from `reports/artifacts/` by `make stats` (with `--report`, see below), which fills the marked tables. Nothing between the `AUTO` markers is typed by hand. A table that still says "Not produced yet" means the run it needs has not happened.

**Reproduce, in order** (the test split runs once, after the `b3-frozen` tag, CLAUDE.md rule 5):

```bash
make eval CONFIG=b0 SPLIT=test FINAL=1     # repeat for b1 b2 a1 a2 a3 b3
python -m src.eval.stats --split test --final --report      # stats_test.json + the tables below
python -m src.eval.failure_labels sheet --config b3 --split dev
# a person labels every line of reports/failure_labels_dev.jsonl
python -m src.eval.failure_labels count --split dev
python -m src.eval.stats --split test --final --report      # refresh, now with failure counts
```

Until the final run, the same commands with `--split dev` (and no `--final`) fill these tables from the dev split, for checking the pipeline. This report's final version must show the test split.

## 1. Run

<!-- BEGIN AUTO:run_info -->
Split: **dev**. Statistics computed 2026-10-11T02:29:10+00:00 by `make stats SPLIT=dev`.

| Config | Eval file | Commit |
|---|---|---|
| B0 | `reports/artifacts/eval_b0_dev.json` | `8c790cb` |
| B1 | `reports/artifacts/eval_b1_dev.json` | `8c790cb` |
| B2 | `reports/artifacts/eval_b2_dev.json` | `8c790cb` |
| A1 | `reports/artifacts/eval_a1_dev.json` | `8c790cb` |
| A2 | `reports/artifacts/eval_a2_dev.json` | `8c790cb` |
| A3 | `reports/artifacts/eval_a3_dev.json` | `8c790cb` |
| B3 | `reports/artifacts/eval_b3_dev.json` | `8c790cb` |
<!-- END AUTO:run_info -->

<!-- Adinath: state the date of the single test run and the b3-frozen commit here, in plain words. -->

## 2. Baselines and configurations

B0 answers from the model's memory with no retrieval (closed book). B1 is BM25 only. B2 is dense retrieval over fixed chunks. B3 is the final system: structure-aware chunks, hybrid retrieval, the reranker and the answer checks. A1 to A3 are the steps between B2 and B3 (section 3).

How each metric is defined:

- **Correctness:** an answerable item is correct when the answer is judged to match the gold answer; an unanswerable item is correct when the system refuses (`src/eval/answer_metrics.py`).
- **Citation validity:** every cited chunk was retrieved for that answer. Not applicable to B0, which retrieves nothing.
- **Correct refusal:** share of unanswerable items the system refused. Higher is better.
- **Over-refusal:** share of answerable items the system refused. Lower is better.

Every rate has a 95% percentile bootstrap interval over items (10,000 resamples, seed 42). Items where a metric does not apply are left out, and n says how many items each rate is based on.

<!-- BEGIN AUTO:baselines -->
| Config | Correctness | Citation validity | Correct refusal | Over-refusal | Latency p50 / p95 (ms) | Cost per query (USD) |
|---|---|---|---|---|---|---|
| B0 | 0.533 [0.366, 0.700] (n=30) | - | 0.833 [0.500, 1.000] (n=6) | 0.208 [0.042, 0.375] (n=24) | 1414.4 / 1905.3 | 6.4e-05 |
| B1 | 0.700 [0.533, 0.867] (n=30) | 1.000 [1.000, 1.000] (n=19) | 1.000 [1.000, 1.000] (n=6) | 0.208 [0.042, 0.375] (n=24) | 1204.3 / 1744.5 | 0.000524 |
| B2 | 0.733 [0.567, 0.867] (n=30) | 1.000 [1.000, 1.000] (n=21) | 1.000 [1.000, 1.000] (n=6) | 0.125 [0.000, 0.250] (n=24) | 1429.2 / 4766.5 | 0.000537 |
| A1 | 0.633 [0.467, 0.800] (n=30) | 1.000 [1.000, 1.000] (n=18) | 1.000 [1.000, 1.000] (n=6) | 0.250 [0.083, 0.417] (n=24) | 1361.4 / 3751.6 | 0.000428 |
| A2 | 0.700 [0.533, 0.867] (n=30) | 1.000 [1.000, 1.000] (n=20) | 1.000 [1.000, 1.000] (n=6) | 0.167 [0.042, 0.333] (n=24) | 1355.4 / 1919.3 | 0.000424 |
| A3 | 0.767 [0.600, 0.900] (n=30) | 1.000 [1.000, 1.000] (n=22) | 1.000 [1.000, 1.000] (n=6) | 0.083 [0.000, 0.208] (n=24) | 1765.7 / 2591.8 | 0.000444 |
| B3 | 0.767 [0.600, 0.900] (n=30) | 1.000 [1.000, 1.000] (n=19) | 1.000 [1.000, 1.000] (n=6) | 0.208 [0.042, 0.375] (n=24) | 3800.6 / 6852.9 | 0.00069 |

Rates with 95% bootstrap intervals. Source: `reports/artifacts/stats_dev.json`.
<!-- END AUTO:baselines -->

### Correctness by question type

The golden set has six kinds of question (report 03). Splitting correctness by kind shows whether the system is only good at the easy ones: closed-book B0, dense retrieval B2 and the final system B3, side by side.

<!-- BEGIN AUTO:by_category -->
| Question type | Items | B0 correctness | B2 correctness | B3 correctness |
|---|---|---|---|---|
| single_hop | 11 | 0.545 [0.273, 0.818] (n=11) | 0.909 [0.727, 1.000] (n=11) | 1.000 [1.000, 1.000] (n=11) |
| multi_hop | 4 | 0.500 [0.000, 1.000] (n=4) | 0.500 [0.000, 1.000] (n=4) | 0.750 [0.250, 1.000] (n=4) |
| comparative | 3 | 0.333 [0.000, 1.000] (n=3) | 0.667 [0.000, 1.000] (n=3) | 0.667 [0.000, 1.000] (n=3) |
| temporal | 3 | 0.667 [0.000, 1.000] (n=3) | 0.667 [0.000, 1.000] (n=3) | 0.333 [0.000, 1.000] (n=3) |
| unanswerable | 6 | 0.833 [0.500, 1.000] (n=6) | 1.000 [1.000, 1.000] (n=6) | 1.000 [1.000, 1.000] (n=6) |
| ambiguous | 3 | 0.000 [0.000, 0.000] (n=3) | 0.000 [0.000, 0.000] (n=3) | 0.000 [0.000, 0.000] (n=3) |

Each type has only a few items, so its intervals are wide: read them as a pointer to where the system is weak, not as a finding on their own.
<!-- END AUTO:by_category -->

## 3. Ablation: one change at a time from B2 to B3

Each row changes one thing and compares correctness on the items both runs graded, paired by item id. The difference has a paired bootstrap 95% interval, and McNemar's exact test uses only the items where the two runs disagree.

<!-- BEGIN AUTO:ablation -->
| Change | Correctness | Difference [95% CI] | Only before / only after correct | McNemar p (exact) |
|---|---|---|---|---|
| B2 -> A1 | 0.733 -> 0.633 (n=30) | -0.100 [-0.233, +0.000] | 3 / 0 | 0.2500 |
| A1 -> A2 | 0.633 -> 0.700 (n=30) | +0.067 [+0.000, +0.167] | 0 / 2 | 0.5000 |
| A2 -> A3 | 0.700 -> 0.767 (n=30) | +0.067 [+0.000, +0.167] | 0 / 2 | 0.5000 |
| A3 -> B3 | 0.767 -> 0.767 (n=30) | +0.000 [-0.100, +0.100] | 1 / 1 | 1.0000 |
| B0 -> B3 | 0.533 -> 0.767 (n=30) | +0.233 [+0.067, +0.400] | 1 / 8 | 0.0391 |
| B2 -> B3 | 0.733 -> 0.767 (n=30) | +0.033 [-0.133, +0.200] | 3 / 4 | 1.0000 |

An interval that contains 0, or p >= 0.05, means the change is not shown to matter.
<!-- END AUTO:ablation -->

## 4. B0 leakage: the questions closed-book B0 got wrong

A model can answer some questions from what it learned in training, so a high score could be memory rather than retrieval. On the items B0 got wrong, the model's memory alone failed; a system that scores well there is gaining from the documents.

<!-- BEGIN AUTO:leakage -->
Items closed-book B0 got wrong: 14.

| Config | Correctness on the B0-incorrect subset |
|---|---|
| B0 | 0.000 [0.000, 0.000] (n=14) |
| B1 | 0.500 [0.214, 0.786] (n=14) |
| B2 | 0.643 [0.429, 0.857] (n=14) |
| A1 | 0.500 [0.214, 0.786] (n=14) |
| A2 | 0.571 [0.286, 0.786] (n=14) |
| A3 | 0.571 [0.286, 0.786] (n=14) |
| B3 | 0.571 [0.286, 0.786] (n=14) |
<!-- END AUTO:leakage -->

## 5. Labelled failures

Each failed item of the B3 run (an incorrect answer, an over-refusal, or an answer to an unanswerable question) was labelled by a person with one category from DELIVERABLES section 4: retrieval_miss, rank_failure, chunk_boundary, extraction_failure, stale_content, reranker_demotion, generator_ignored_context, invalid_citation, over_refusal, hallucination. The labels are in `reports/failure_labels_<split>.jsonl`; no label was written by AI.

<!-- BEGIN AUTO:failures -->
_Not produced yet: failure labels. Run `python -m src.eval.failure_labels count --split dev`._
<!-- END AUTO:failures -->

<!-- Adinath: after labelling, add two or three sentences on the most common categories and one example of each, quoting the item id. -->

## 6. Known limitations

- **Small test set.** The test split is 40% of 50 items, so intervals are wide and a real difference can fail to reach significance. Per-category results rest on a few items each.
- **Correctness is judged by an LLM.** How the judge was checked against human judgments is in report 05.
- **One run per configuration.** Generation is not perfectly repeatable, so a rerun could change a few items.
- **Corpus limits** (report 01): no faa.gov pages, the CFR is the January 1, 2025 edition, and OCR text from handouts is imperfect.
