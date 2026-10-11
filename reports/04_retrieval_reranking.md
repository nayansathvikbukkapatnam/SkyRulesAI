# 04 Retrieval and reranking

**Owner:** Sai (BM25, hybrid fusion, reranker, retrieval metrics, this report).

Every number in this report is generated from `reports/artifacts/` by `python -m src.reports.retrieval`, which fills the marked tables. Nothing between the `AUTO` markers is typed by hand. A table that still says "Not produced yet" means the run it needs has not happened. All tuning and every number here use the **dev split**; the test split runs once, on Sunday, and is reported in 06.

**Reproduce, in order** (after `make split`):

```bash
make index STRATEGY=fixed                     # B1, B2 search fixed@v1
make index STRATEGY=struct                    # A1, A2, A3 search struct@v1 (both live in one collection)
make retrieval-eval CONFIG=b1 SPLIT=dev       # BM25, fixed chunks
make retrieval-eval CONFIG=b2 SPLIT=dev       # dense, fixed chunks
make retrieval-eval CONFIG=a1 SPLIT=dev       # dense, structure-aware chunks
make retrieval-eval CONFIG=a2 SPLIT=dev       # hybrid (dense + BM25, RRF)
make retrieval-eval CONFIG=a3 SPLIT=dev       # hybrid + cross-encoder rerank
make eval CONFIG=a3 SPLIT=dev                 # answers, for the gate sweep
python -m src.eval.tune_gate --config a3      # gate threshold, dev only
python -m src.reports.retrieval --demo-filter # filter demonstration + fill the tables
```

## 1. How retrieval works

| Stage | Method | Code |
|---|---|---|
| Dense | bge-small-en-v1.5 query embedding (with bge's retrieval instruction), cosine similarity in Chroma | `src/index/store.py` |
| Keyword | BM25Okapi over the same chunk file; lowercase alphanumeric tokens, so "Part 107", "part-107" and "PART 107" match | `src/retrieve/bm25.py` |
| Hybrid | Reciprocal Rank Fusion of a 20-candidate pool from each side: `fused = Σ 1 / (60 + rank)` | `src/retrieve/retrieve.py` |
| Rerank | Cross-encoder `ms-marco-MiniLM-L-6-v2` reads the query and each pooled chunk together and keeps the top 5 | `src/retrieve/rerank.py` |

**Why RRF:** cosine similarities (0 to 1) and BM25 scores (unbounded) are on different scales, so adding them would let one side dominate. RRF uses only each chunk's rank in each list. A chunk ranked first by both sides scores 2/61; a chunk found by one side only still enters the pool.

**Why a reranker:** the embedding model encodes the query and the chunk separately; the cross-encoder reads them together, which judges relevance better but is too slow to run over all chunks. It only reorders the pool, so the pool's Recall@20 is its ceiling.

## 2. Metrics

A retrieved chunk is a **hit** for a golden item when it comes from the item's `source_url` and its normalized text contains the item's `evidence_quote` (case, curly quotes, dashes and whitespace normalized; nothing else). Multi-page items have one evidence pair per page. Recall@k is the share of an item's pairs hit in the top k; MRR is 1 / rank of the first hit; nDCG@10 uses binary gains, one per evidence pair. Unanswerable items have no evidence and are not scored here; their refusal behavior is in report 05. Code: `src/eval/retrieval_metrics.py`.

A quote split across two chunks counts as a miss. That is the "chunk boundary" failure in the error analysis (06).

## 3. Dense vs BM25 vs hybrid, with intervals

<!-- BEGIN AUTO:modes -->
| Config | Retrieval | Chunks | Rerank | Recall@5 | Recall@20 (pool) | MRR | nDCG@10 |
|---|---|---|---|---|---|---|---|
| B1 | bm25 | `fixed@v1` | no | 0.3913 [0.2174, 0.587] | 0.5362 [0.3478, 0.7174] | 0.4063 [0.2259, 0.5926] | 0.389 [0.212, 0.5735] |
| B2 | dense | `fixed@v1` | no | 0.4203 [0.2391, 0.6087] | 0.5652 [0.3696, 0.7391] | 0.4087 [0.2294, 0.5949] | 0.4177 [0.2549, 0.5899] |
| A1 | dense | `struct@v1` | no | 0.3551 [0.1812, 0.5362] | 0.5145 [0.3261, 0.6957] | 0.3659 [0.1924, 0.5493] | 0.3319 [0.1678, 0.5039] |
| A2 | hybrid | `struct@v1` | no | 0.4928 [0.3043, 0.6739] | 0.558 [0.3768, 0.7391] | 0.4352 [0.2588, 0.6139] | 0.4391 [0.2683, 0.6105] |
| A3 | hybrid | `struct@v1` | yes | 0.5072 [0.3116, 0.6957] | 0.558 [0.3768, 0.7391] | 0.3928 [0.2275, 0.5652] | 0.4103 [0.2435, 0.5736] |

Dev split, 23 answerable items scored per config (unanswerable items have no evidence to find). Each cell is the mean with its 95% bootstrap interval (10,000 resamples over items, seed 42). Recall@20 is the 20-candidate pool; the other metrics are on the list the generator sees (the reranked top 5 for A3). Sources: `reports/artifacts/retrieval_<config>_dev.json`.
<!-- END AUTO:modes -->

## 4. Reranker: lift, ceiling and added latency

A2 and A3 differ only in the reranker, so this isolates its effect. The change is the mean per-item difference with a 95% paired bootstrap interval (same items, resampled together).

<!-- BEGIN AUTO:rerank -->
| Metric (dev) | A2 (no rerank) | A3 (rerank top 5) | Change [95% paired CI] |
|---|---|---|---|
| Recall@5 | 0.4928 | 0.5072 | +0.0145 [-0.087, 0.1304] |
| MRR | 0.4352 | 0.3928 | -0.0424 [-0.1217, 0.0315] |
| nDCG@10 | 0.4391 | 0.4103 | -0.0287 [-0.1062, 0.0555] |

**Ceiling:** the reranker only reorders the pool, so Recall@20 of the pool, 0.558, is the most Recall@5 it can reach.

**Added latency (cross-encoder, CPU):** p50 420.8 ms, p95 438.6 ms per query, over 23 dev items.

Sources: `reports/artifacts/retrieval_a2_dev.json`, `retrieval_a3_dev.json`.
<!-- END AUTO:rerank -->

## 5. Metadata filtering

`retrieve(..., filters={"page_type": ...})` applies the same filter to the dense side (a Chroma `where` clause) and the BM25 side (a mask), so a filtered query can only return chunks of the requested types. The queries below are fixed demonstration queries, not golden items.

<!-- BEGIN AUTO:filter_demo -->
**How much does it cost to register a drone?**, filter `page_type = guide_page` (all results match):

| Rank | page_type | Title |
|---|---|---|
| 1 | `guide_page` | Fly for Work |
| 2 | `guide_page` | Comply and Fly |
| 3 | `guide_page` | Comply and Fly |
| 4 | `guide_page` | Comply and Fly |
| 5 | `guide_page` | Comply and Fly |

**How much does it cost to register a drone?**, filter `page_type = regulation_summary` (all results match):

| Rank | page_type | Title |
|---|---|---|
| 1 | `regulation_summary` | Registration and Marking Requirements for Small Unmanned Aircraft (Fed |
| 2 | `regulation_summary` | Operation and Certification of Small Unmanned Aircraft Systems (Federa |
| 3 | `regulation_summary` | Registration and Marking Requirements for Small Unmanned Aircraft (Fed |
| 4 | `regulation_summary` | Operation and Certification of Small Unmanned Aircraft Systems (Federa |
| 5 | `regulation_summary` | Remote Identification of Unmanned Aircraft Systems (Federal Register P |

**What are the operating limitations for small unmanned aircraft?**, filter `page_type = toolkit_pdf` (all results match):

| Rank | page_type | Title |
|---|---|---|
| 1 | `toolkit_pdf` | KBYF_IndexCards2025_v3 |
| 2 | `toolkit_pdf` | No Drone Zone - Public Safety Small Drone Playbook |
| 3 | `toolkit_pdf` | https://knowbeforeyoufly.org/wp-content/uploads/2025/12/KBYF-Unit-of-S |
| 4 | `toolkit_pdf` | No Drone Zone - Public Safety Small Drone Playbook |
| 5 | `toolkit_pdf` | HAS JUST LANDED |

Hybrid retrieval on `struct@v1`, commit `a872426`. Source: `reports/artifacts/filter_demo.json`.
<!-- END AUTO:filter_demo -->

## 6. Evidence gate threshold (B3)

B3 refuses when the best reranked chunk scores below `gate_threshold`. The threshold is chosen on dev only, by replaying the A3 dev run (`trace.gate_score` is recorded even with the gate off) at every observed score. Code: `src/eval/tune_gate.py`.

<!-- BEGIN AUTO:gate -->
Chosen `gate_threshold`: **-inf** (rule: maximize dev correctness; ties: lower over-refusal, then lower threshold), from `eval_a3_dev.json` at commit `8c790cb`, 30 dev items.

| Dev rate | No gate | Chosen gate |
|---|---|---|
| Correctness | 0.7667 | 0.7667 |
| Over-refusal | 0.0833 | 0.0833 |
| Correct refusal | 1 | 1 |

Source: `reports/artifacts/gate_tuning_dev.json`.
<!-- END AUTO:gate -->

No threshold raised dev correctness, so the rule ends at no gate (−inf). `configs/b3.yaml` uses −4.6741, the lowest finite threshold tied for best (#88): on dev it gives the same results as no gate (it stops only g041, which was already refused). The gate is therefore a floor for very weak retrievals, not a source of B3's results.

## 7. Findings

_To write after the tables above are filled, from those numbers only:_

- _Which retrieval mode is best on dev, and whether the intervals overlap._
- _The reranker's lift, whether its paired interval excludes 0, how close it gets to the pool ceiling, and what it costs in latency._
- _What the gate threshold buys (correct refusals) and costs (over-refusal)._

## 8. Limitations

- The dev split is small, so intervals are wide; differences inside overlapping intervals are not claimed as improvements.
- The hit rule needs the exact evidence quote inside one chunk, so a correct chunk that paraphrases or splits the quote counts as a miss.
- The reranker is a general web-search model (MS MARCO), not trained on regulations.
- The gate is tuned on A3's answers without checks; B3 adds the answer checks, which also refuse, so B3's final refusal rates are reported in 05 and 06.
