# Golden set format (`eval/golden.jsonl`)

One JSON object per line. Items are written by team members from the live FAA pages and verified by a different member against the page (CLAUDE.md rule 4). Claude Code does not write items.

| Field | Required for | Notes |
|---|---|---|
| `id` | all | Unique, e.g. `dorai-01` |
| `category` | all | `single_hop`, `multi_hop`, `comparative`, `temporal`, `unanswerable`, `ambiguous` |
| `question` | all | As a real user would ask it |
| `gold_answer` | all except `unanswerable` | Taken from the page, not from memory |
| `source_url` | `single_hop`, `multi_hop`, `comparative`, `temporal` (optional for `ambiguous`) | The faa.gov page the answer is on. For items that combine pages, a list of URLs |
| `evidence_quote` | same as `source_url` | 25 words or fewer, copied exactly from the page. For a list of URLs, a list of quotes in the same order |
| `searched_for` | `unanswerable` | What you searched for and where, to show the answer is absent |
| `author` | all | Your first name |
| `verified_by` | all | First name of the teammate who checked it against the page (not the author) |
| `verified_date` | all | `YYYY-MM-DD` |

`multi_hop` items need at least 2 different pages.

Shape only (placeholders, not a real item):

```json
{"id": "<name>-01", "category": "single_hop", "question": "<question>", "gold_answer": "<answer from the page>", "source_url": "https://www.faa.gov/uas/<page>", "evidence_quote": "<exact words from the page>", "author": "<name>", "verified_by": "<other name>", "verified_date": "2026-10-10"}
```

Check the file with `make golden-check`. Until `data/normalized/` exists, `make golden-check NO_QUOTES=1` checks everything except whether each quote appears on its saved page. The full check must pass before the split (`make split`).

Results go to `reports/artifacts/golden_check.json` (counts per category, author and verifier) for `reports/03_golden_set.md`.
