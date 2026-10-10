# Golden set format (`eval/golden.jsonl`)

One JSON object per line. Items are written by team members from the live FAA pages and verified by a different member against the page (CLAUDE.md rule 4). Claude Code does not write items.

| Field | Required for | Notes |
|---|---|---|
| `id` | all | Unique, `g001` to `g050` (zero-padded number, no names) |
| `category` | all | `single_hop`, `multi_hop`, `comparative`, `temporal`, `unanswerable`, `ambiguous` |
| `question` | all | As a real user would ask it |
| `gold_answer` | all except `unanswerable` | Taken from the page, not from memory |
| `source_url` | `single_hop`, `multi_hop`, `comparative`, `temporal` (optional for `ambiguous`) | The page or document the answer is on, from an approved source (CLAUDE.md, October 10 section): an eCFR section, a Federal Register document, a GovInfo PDF or a Know Before You Fly guidance page. For items that combine pages, a list of URLs |
| `evidence_quote` | same as `source_url` | 25 words or fewer, copied exactly from the page. For a list of URLs, a list of quotes in the same order |
| `searched_for` | `unanswerable` | What you searched for and where, to show the answer is absent |
| `author` | all | Your first name |
| `verified_by` | all | First name of the teammate who checked it against the page (not the author) |
| `verified_date` | all | `YYYY-MM-DD` |

`multi_hop` items need at least 2 different pages.

Shape only (placeholders, not a real item):

```json
{"id": "g001", "category": "single_hop", "question": "<question>", "gold_answer": "<answer from the page>", "source_url": "https://www.govinfo.gov/content/pkg/CFR-2025-title14-vol2/xml/CFR-2025-title14-vol2-sec107-<number>.xml", "evidence_quote": "<exact words from the page>", "author": "<name>", "verified_by": "<other name>", "verified_date": "2026-10-10"}
```

Check the file with `make golden-check`. Until `data/normalized/` exists, `make golden-check NO_QUOTES=1` checks everything except whether each quote appears on its saved page. The full check must pass before the split (`make split`).

Results go to `reports/artifacts/golden_check.json` (counts per category, author and verifier) for `reports/03_golden_set.md`.

## Verifying items (Pavan, Nayan, Sai, Adinath)

Dorai wrote every item, so Dorai verifies none. Who checks which items is in
`eval/verification_assignments.json` (13, 13, 12 and 12 items, in blocks).

For each item you are assigned:

1. See it next to the saved page text: `.venv/bin/python -m src.eval.show_item --name <you>`
   (or one item: `... src.eval.show_item g003`). It is read-only.
2. Open the item's `source_url` in a browser and confirm that **the gold answer is right** and
   **the evidence quote is on the page, word for word**. For an unanswerable item, repeat the
   search in `searched_for` (`grep -ril "<term>" data/normalized`) and confirm the answer is
   really not in the corpus.
3. If it is right, set `verified_by` to your first name (for example `"Pavan"`) and
   `verified_date` to today (`YYYY-MM-DD`) on that item's line in `eval/golden.jsonl`.
4. If something is wrong or unclear, **do not edit the question, answer or quote**. Leave
   `verified_by` empty and tell Dorai which item and why.

Only change `verified_by` and `verified_date`, and only on your own items. Then run
`make golden-check`: your items should no longer be in its error list.
