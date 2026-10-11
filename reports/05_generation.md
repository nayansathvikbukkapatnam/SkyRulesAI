# 05. Generation, grounding and refusal

Owner: Pavan. **Draft.** The text describes the system as built; every number sits in an AUTO block and is filled from `reports/artifacts/` by `python -m src.reports.generation` (rule 6). Blocks that say "not produced yet" are waiting on the dev runs.

## 1. Prompt and grounding design

The generator is Claude Haiku 5.5 through the Claude API (`src/generate/llm.py`), called by `generate()` in `src/generate/generate.py` with the prompt in `src/generate/prompt.py`.

- **Numbered passages, cited per sentence.** The top reranked passages are numbered inside `<passage>` tags. The model replies with JSON only: `{"sentences": [{"text", "passages": [n]}]}` or `{"insufficient": true}`. `generate()` maps passage numbers to chunk IDs; a number outside the list keeps an `invalid-passage-N` marker so the citation check fails it instead of the citation silently disappearing.
- **Answer only from the passages.** The system prompt forbids facts from memory, asks for numbers and dates copied exactly, and says to return `insufficient` when the passages do not cover the question.
- **Untrusted passages.** Passage text is data, never instructions; tag-like text inside a passage is neutralized so it cannot close its own tag.
- **Short, paraphrased sentences.** Each sentence under 25 words, never more than 20 consecutive words copied, which backs up the 25-word quote check.
- **One repair, then fail closed.** Unparseable JSON gets one repair call; a truncated reply, a refusal by the model or JSON still broken after the repair is an error, and `answer()` refuses.
- **Closed book (B0)** uses a separate prompt with no passages and no citations.

Generation settings:

<!-- BEGIN AUTO:settings -->
| Setting | Value | Note |
|---|---|---|
| temperature | null (not sent) | Haiku 5.5 rejects any value but 1 |
| thinking | disabled | off, so the answer is the reply text only |
| effort | medium |  |
| max_tokens | 1024 | a reply cut off here is refused (truncated) |
| price per million input / output tokens (USD) | 0.1 / 0.5 | Claude API, prompts under 100000 tokens |

Source: `generation` in `configs/default.yaml` (the frozen B3 config).
<!-- END AUTO:settings -->

## 2. The answer pipeline and its checks

`answer()` (`src/generate/answer.py`) runs: follow-up rewrite (only with history), router, retrieve and rerank, evidence gate, generate, checks, then citations, latency and cost. Any of these ends in a refusal with a stated reason: injected instructions only, the evidence gate, `insufficient`, a generation error, a failed check, or a check that could not run. Infrastructure errors (a missing index) raise instead, so evaluation counts them as errors, not refusals.

B3 runs four checks on every sentence: citation validity, the 25-word quote limit (`src/generate/checks.py`), claim support by an LLM judge and number and date matching (`src/generate/claim_check.py`). B2 runs none; the compare view shows which B3 checks each B2 sentence would fail.

Answer rates per config:

<!-- BEGIN AUTO:rates -->
| Config | Items | Answered | Errors | Correct | Citation valid | Correct refusal (unanswerable) | Over-refusal (answerable) |
|---|---|---|---|---|---|---|---|
| B0 | 30 | 19 | 0 | 0.4667 (n=30) | n/a | 0.8333 (n=6) | 0.25 (n=24) |
| B1 | 30 | 19 | 0 | 0.7 (n=30) | 1 (n=19) | 1 (n=6) | 0.2083 (n=24) |
| B2 | 30 | 21 | 0 | 0.7667 (n=30) | 1 (n=21) | 1 (n=6) | 0.125 (n=24) |
| A1 | 30 | 19 | 0 | 0.7333 (n=30) | 1 (n=19) | 1 (n=6) | 0.2083 (n=24) |
| A2 | 30 | 20 | 0 | 0.7 (n=30) | 1 (n=20) | 1 (n=6) | 0.1667 (n=24) |
| A3 | 30 | 22 | 0 | 0.7667 (n=30) | 1 (n=22) | 1 (n=6) | 0.0833 (n=24) |
| B3 | 30 | 20 | 0 | 0.7 (n=30) | 1 (n=20) | 1 (n=6) | 0.1667 (n=24) |

Source: `reports/artifacts/eval_<config>_dev.json` (`make eval CONFIG=<config> SPLIT=dev`), commits: A1 e955c7f, A2 e955c7f, A3 e955c7f, B0 e955c7f, B1 e955c7f, B2 e955c7f, B3 e955c7f. Citation validity is not applicable to B0 (closed book cites nothing). Intervals and paired tests are in report 06.
<!-- END AUTO:rates -->

## 3. Refusals: correct refusals and over-refusals

Correct refusal is measured on unanswerable items, over-refusal on answerable ones (table above). Why B3 refused:

<!-- BEGIN AUTO:refusals -->
| Refusal reason | Refusals | On unanswerable items | On answerable items |
|---|---|---|---|
| Not enough support (model said insufficient) | 8 | 5 | 3 |
| Failed check: Numbers, Claims | 1 | 0 | 1 |
| Evidence gate | 1 | 1 | 0 |

Source: `trace.refusal_reason` in `eval_b3_dev.json`. 10 refusals; 0 pipeline errors are counted separately.
<!-- END AUTO:refusals -->

## 4. Citation validity and the quote limit

Each answer that reaches the checks, and what failed. A failed check refuses the whole answer, so no failed sentence reaches a user.

<!-- BEGIN AUTO:checks -->
| Check | Answers checked | Answers failed (refused) | Sentences failed |
|---|---|---|---|
| Citations | 21 | 0 | 0 |
| Claims | 21 | 1 | 3 |
| Numbers | 21 | 1 | 3 |
| Quotes (25-word limit) | 21 | 0 | 1 |

Source: `trace.checks` in `eval_b3_dev.json`. Any failed check refuses the whole answer (fail closed), so no failed sentence was shown to a user.
<!-- END AUTO:checks -->

## 5. Referral behavior

The router (`src/generate/router.py`) tags location-specific and high-stakes questions and attaches a referral to B4UFLY, the UAS Facility Maps or the FAA, also when SkyRules refuses.

<!-- BEGIN AUTO:referrals -->
| Router tag | Questions |
|---|---|
| normal | 24 |
| high_stakes | 2 |
| location_live | 2 |
| pilot_unclear | 2 |

| Router tag | Referral | Answer status | Questions |
|---|---|---|---|
| high_stakes | high_stakes | answered | 2 |
| location_live | location | refused | 2 |

Source: `router_tag` and `referral` in `eval_b3_dev.json`.
<!-- END AUTO:referrals -->

## 6. How answer correctness is judged, and how the judge was checked

Correctness on answerable items is graded by an LLM judge (`src/eval/answer_metrics.py`) that compares the answer with the human-written gold answer; a refusal is incorrect, and a judge error counts as incorrect. Unanswerable items are correct when refused.

To check the judge, 20 answered dev items were sampled with a fixed seed and labelled by hand, blind to the judge's verdict (`python -m src.reports.generation label-sheet`), then compared (`agreement`):

<!-- BEGIN AUTO:judge -->
_Not produced yet: the judge check against hand labels. Run `python -m src.reports.generation label-sheet --split dev, label it, then python -m src.reports.generation agreement --split dev`._
<!-- END AUTO:judge -->

## 7. Latency and cost

<!-- BEGIN AUTO:latency_cost -->
| Config | route p50 / p95 (ms) | retrieve p50 / p95 (ms) | rerank p50 / p95 (ms) | generate p50 / p95 (ms) | check p50 / p95 (ms) | total p50 / p95 (ms) | Cost per query (USD) | Total (USD) |
|---|---|---|---|---|---|---|---|---|
| B0 | 0.1 / 0.2 | 0 / 0 | 0 / 0 | 1145.4 / 1815.3 | 0 / 0 | 1147.3 / 1817 | 0.000066 | 0.001986 |
| B1 | 0.1 / 0.3 | 18 / 34.1 | 0 / 0 | 1178.8 / 1779.5 | 0 / 0 | 1197.9 / 1814.9 | 0.000528 | 0.015844 |
| B2 | 0.1 / 0.3 | 36.1 / 161.5 | 0 / 0 | 1205.1 / 1659 | 0 / 0 | 1243.4 / 2367.7 | 0.000537 | 0.016099 |
| A1 | 0.1 / 0.3 | 38.1 / 172.5 | 0 / 0 | 1067.6 / 1726.2 | 0 / 0 | 1122.9 / 3007.6 | 0.000428 | 0.012838 |
| A2 | 0.1 / 0.2 | 47.2 / 185.2 | 0 / 0 | 1157 / 1634.2 | 0 / 0 | 1207.3 / 6261.4 | 0.000427 | 0.012813 |
| A3 | 0.1 / 0.3 | 47.2 / 171 | 329.8 / 482.1 | 1223.2 / 1643.9 | 0 / 0 | 1624.7 / 2167.7 | 0.000439 | 0.013172 |
| B3 | 0.1 / 0.2 | 47.3 / 55.9 | 326 / 490 | 1216.2 / 1792.6 | 1832.7 / 4614.6 | 3414 / 8066.8 | 0.0007 | 0.020987 |

Source: `summary.latency_ms` and `summary.cost_usd_*` in `eval_<config>_dev.json`. Latency is measured on one laptop (CPU retrieval and rerank, Claude API calls); a cached LLM reply keeps its original latency.
<!-- END AUTO:latency_cost -->

## 8. Limitations

- The model rejects a temperature setting, so generation is not guaranteed deterministic; LLM replies are cached by request, which makes re-runs repeatable on one laptop.
- The judge is the same model family as the generator; the hand-label check above is the evidence for how far it can be trusted.
- Latency comes from one laptop and includes CPU retrieval and reranking.
