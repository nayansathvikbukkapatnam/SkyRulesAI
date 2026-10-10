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
_Not produced yet: eval runs on dev. Run `make eval CONFIG=b3 SPLIT=dev`._
<!-- END AUTO:rates -->

## 3. Refusals: correct refusals and over-refusals

Correct refusal is measured on unanswerable items, over-refusal on answerable ones (table above). Why B3 refused:

<!-- BEGIN AUTO:refusals -->
_Not produced yet: the B3 eval run. Run `make eval CONFIG=b3 SPLIT=dev`._
<!-- END AUTO:refusals -->

## 4. Citation validity and the quote limit

Each answer that reaches the checks, and what failed. A failed check refuses the whole answer, so no failed sentence reaches a user.

<!-- BEGIN AUTO:checks -->
_Not produced yet: the B3 eval run. Run `make eval CONFIG=b3 SPLIT=dev`._
<!-- END AUTO:checks -->

## 5. Referral behavior

The router (`src/generate/router.py`) tags location-specific and high-stakes questions and attaches a referral to B4UFLY, the UAS Facility Maps or the FAA, also when SkyRules refuses.

<!-- BEGIN AUTO:referrals -->
_Not produced yet: the B3 eval run. Run `make eval CONFIG=b3 SPLIT=dev`._
<!-- END AUTO:referrals -->

## 6. How answer correctness is judged, and how the judge was checked

Correctness on answerable items is graded by an LLM judge (`src/eval/answer_metrics.py`) that compares the answer with the human-written gold answer; a refusal is incorrect, and a judge error counts as incorrect. Unanswerable items are correct when refused.

To check the judge, 20 answered dev items were sampled with a fixed seed and labelled by hand, blind to the judge's verdict (`python -m src.reports.generation label-sheet`), then compared (`agreement`):

<!-- BEGIN AUTO:judge -->
_Not produced yet: the judge check against hand labels. Run `python -m src.reports.generation label-sheet --split dev, label it, then python -m src.reports.generation agreement --split dev`._
<!-- END AUTO:judge -->

## 7. Latency and cost

<!-- BEGIN AUTO:latency_cost -->
_Not produced yet: eval runs on dev. Run `make eval CONFIG=b3 SPLIT=dev`._
<!-- END AUTO:latency_cost -->

## 8. Limitations

- The model rejects a temperature setting, so generation is not guaranteed deterministic; LLM replies are cached by request, which makes re-runs repeatable on one laptop.
- The judge is the same model family as the generator; the hand-label check above is the evidence for how far it can be trusted.
- Latency comes from one laptop and includes CPU retrieval and reranking.
