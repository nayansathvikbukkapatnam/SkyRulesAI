# SkyRulesAI

A retrieval-augmented chatbot that answers questions about U.S. drone rules from official sources only, cites a source for every sentence, and says "I don't have that information" when the sources do not cover a question. MSAI 633 residency project.

The design is in [reports/00_proposal.md](reports/00_proposal.md); see its §15 for the changes made after submission. The definition of "finished" is in [docs/DELIVERABLES.md](docs/DELIVERABLES.md).

## Team

| Name | GitHub | Role | Owns |
|---|---|---|---|
| Nayan Sathvik Bukkapatnam | @nayansathvikbukkapatnam | Software Developer | Foundation, embeddings, vector database, re-chunk migration, answer checks, CI; reports 02 and README |
| Pavan Krishna Lankipalli | @pavan2564 | Full Stack Engineer | LLM client, generation, app; report 05 |
| Dorai Saketh Guntur | @sakethguntur | Product Manager | Golden set, safety router, ethics, demo; reports 03 and 07 |
| Sai Praneeth Kondaveeti | @saipraneeth112698 | AI Engineer | Chunking, BM25 and hybrid retrieval, reranking, claim check, retrieval metrics; report 04 |
| Adinath Subash Kadam | @AdinathKadam | Senior Systems Analyst | Crawl, extraction, statistics; reports 01 and 06 |

## How it works

**Build the knowledge base once:**

1. **Crawl** the approved sources at no more than 1 request per second, checking robots.txt first, and save raw bytes with their SHA-256 (`src/crawl/`).
2. **Extract** clean text, headings and tables to `data/normalized/` (`src/extract/`).
3. **Chunk** two ways: `fixed@v1` (512-token windows) and `struct@v1` (one section per chunk, heading path included) (`src/chunk/`).
4. **Embed and index:** bge-small-en-v1.5 vectors, normalized, in Chroma with cosine distance, plus a BM25 keyword index (`src/index/`, `src/retrieve/bm25.py`).
5. **Re-chunk** from saved text without re-crawling, with an orphan check (`src/index/migrate.py`).

**Answer each question:**

1. Rewrite follow-up questions, and route location, high-stakes and injection-like questions.
2. Retrieve: dense plus BM25, fused with Reciprocal Rank Fusion, a pool of 20 (`src/retrieve/retrieve.py`).
3. Rerank to the top 5 with a cross-encoder (`src/retrieve/rerank.py`).
4. Refuse if the best score is below the evidence gate.
5. Generate: Claude Haiku 5.5 answers only from the 5 chunks, as JSON sentences with chunk IDs (`src/generate/`).
6. Check: citation validity, the 25-word quote limit (`checks.py`), claim support, and number and date match (`claim_check.py`). Failing sentences are dropped, and too many failures, or any check error, means refusal.

## Setup

Needs Python 3.11 (macOS: `brew install python@3.11`) and, for OCR, `brew install tesseract`.

```bash
make setup             # creates .venv and installs the pinned requirements.txt
cp .env.example .env   # then fill in the keys below
make test              # unit tests run on tests/fixtures/, no network, API key or AWS needed
make help              # every pipeline command
```

`.env` keys (never commit `.env`):

| Key | Value |
|---|---|
| `LLM_PROVIDER` | `anthropic` (Claude API, the default) or `bedrock` (AWS Bedrock). Both use the same client in `src/generate/llm.py`. |
| `ANTHROPIC_API_KEY` | Your own key from the SkyRules Claude Console org. Needed when `LLM_PROVIDER=anthropic`. Never commit it or paste it into chats, PRs or issues. |
| `ANTHROPIC_MODEL_ID` | `claude-haiku-5-5` |
| `AWS_REGION`, `AWS_PROFILE`, `BEDROCK_MODEL_ID` | Only when `LLM_PROVIDER=bedrock`. Use a named AWS CLI profile (`aws configure --profile <name>`); never put AWS keys in `.env`. |
| `CRAWL_CONTACT_EMAIL` | Team contact email for the crawler's User-Agent. |

Check LLM access with one live call: `.venv/bin/python -m src.generate.llm --smoke` prints the reply, token counts, latency and cost.

Dependencies: edit `requirements.in`, then run `make freeze` to re-pin `requirements.txt`.

## Reproduce every number

Every number in `reports/` comes from a file in `reports/artifacts/` written by one of these commands. Run them in this order. A command whose stage is not merged yet prints "not implemented yet" and stops.

| Step | Command | Writes |
|---|---|---|
| Crawl (once) | `make crawl` | `data/raw/`, crawl stats |
| Extract | `make extract` | `data/normalized/`, `ingestion_stats.json` |
| Golden set check | `make golden-check` | validation report for `eval/golden.jsonl` |
| Dev/test split (once) | `make split` | `eval/split.json` |
| Fixed chunks and index | `make chunk STRATEGY=fixed` then `make index STRATEGY=fixed` | `chunk_stats.json`, `chunk_hist_fixed@v1.png`, `embedding_info.json` |
| Baselines on dev | `make retrieval-eval CONFIG=b1 SPLIT=dev`, `CONFIG=b2`; `make eval CONFIG=b0 SPLIT=dev` (and `b1`, `b2`) | `retrieval_<config>_dev.json`, `eval_<config>_dev.json` |
| Re-chunk | `make migrate FROM=fixed@v1 TO=struct@v1` then `make chunk STRATEGY=struct` | `migration.json`, `migration_log.txt`, struct chunk stats |
| Ablation on dev | `make retrieval-eval` and `make eval` with `CONFIG=a1`, `a2`, `a3`, `b3` | per-config artifacts |
| Freeze | `git tag b3-frozen` (after tuning on dev only) | the frozen commit |
| Test split, once | `make index STRATEGY=fixed` (B1 and B2 need it again), then `make eval CONFIG=<b0..b3> SPLIT=test FINAL=1` | `eval_<config>_test.json` |
| Statistics | `make stats` | bootstrap intervals, McNemar tests |
| Report 02 tables | `make report-chunking` | fills `reports/02_chunking.md` |
| App | `make app` | http://localhost:8000 |

`make test` and `make lint` run on every pull request in GitHub Actions (`.github/workflows/ci.yml`), using fixtures only: no API keys and no network calls.

## Repository layout

```
src/common/    data contracts (contracts.py), config loading, paths
src/crawl/     crawler, robots.txt, rate limit
src/extract/   HTML, PDF, OCR, PII filter -> data/normalized/
src/chunk/     fixed and structure-aware chunkers, chunk stats
src/index/     embeddings, Chroma store, re-chunk migration
src/retrieve/  BM25, hybrid retrieve(), reranker
src/generate/  LLM client, prompt, router, answer checks, claim check
src/eval/      golden set checker, split, metrics, statistics
src/reports/   fills report tables from artifacts
configs/       default.yaml plus one file per baseline and ablation step
eval/          golden.jsonl, split.json
reports/       00 to 07 and artifacts/
tests/         unit tests and fixtures
```

## Working agreements

- `main` is protected: every change goes through a pull request approved by another member, and force pushes are blocked. CI (lint and tests) runs on every pull request.
- Short-lived branches named by stage (`index/embed-chroma`, `generate/answer-checks`), with present-tense commit messages (`feat: ...`, `fix: ...`).
- Every PR description ends with an AI usage line (see below).
- Secrets live only in your own `.env`. Secret scanning and push protection are on.

## Known limitations

- **Sources changed from the proposal.** faa.gov blocks automated access (403, even for robots.txt), so the corpus is the instructor-approved eCFR (14 CFR Parts 107, 89, 48 and §§ 101.41 to 101.49), Federal Register drone rules with their GovInfo PDFs, and Know Before You Fly guidance pages. The blocked faa.gov requests are kept in the crawl log as evidence; nothing from faa.gov is in the corpus.
- **Know Before You Fly is an FAA partner campaign, not the FAA**, and the UI labels it that way.
- **No temperature control.** Claude Haiku 5.5 does not accept a temperature setting, so answers can vary between runs, unlike the proposal's "temperature 0". LLM responses are cached on disk (`.cache/llm/`, keyed by the full request), so a re-run of the same evaluation reproduces the same answers.
- **A snapshot in time.** The corpus is a single crawl. Rules that changed afterwards are not reflected, so every citation shows the source's date.
- **No live airspace data.** The chatbot explains the rules but cannot say whether a specific place is legal to fly in right now. Location questions are referred to B4UFLY and the UAS Facility Maps.
- **English only**, and a small evaluation set (target 50 items, about 20 in the test split), so results are reported with confidence intervals.
- **LLM-judged metrics.** Answer correctness and claim support use an LLM judge. A human re-checks a sample of the judge's verdicts.

## AI usage

AI assistance is disclosed per milestone, as the course requires. Each pull request also has its own AI usage line naming the files involved and what the member checked; the rows below are compiled from those lines, with the PR numbers, so each claim can be checked against the PR.

| Milestone | Tool | Used for | Human review |
|---|---|---|---|
| Proposal | AI assistant | Brainstorming domains, checking robots.txt of candidate sites, drafting the proposal | The team edited it and owns every design decision |
| Repository setup | AI coding assistant | GitHub CLI commands for branch protection, secret scanning, issues and the project board | Nayan chose the settings and checked each one |
| Foundation, embeddings, vector database, migration, answer checks, CI, report 02 tooling (Nayan) | AI coding assistant | Drafting code and tests in PRs #13, #14, #22, #32, #34 and this one | Nayan reviewed the code, ran the tests and the end-to-end rehearsals, and checked them against the proposal |
| Golden set | none | AI tools never write golden items (team rule 4) | Dorai writes every item; other members verify them against the source |
| Crawl, extraction, OCR, PII filter, corpus, report 01 (Adinath) | Claude Code | Drafted the crawler, robots parser, sources config, extract modules, OCR, PII filter, the report filler and their tests (PRs #28, #29, #36, #38, #41, #49, #60) | Adinath chose the sources and exclusions, approved every dry run and the full crawl, reviewed the extraction spot checks and the corpus numbers |
| Chunking, BM25, hybrid retrieval, reranker, claim check, eval runner, gate tuning, dev runs, reports 02 and 04 (Sai) | Claude Code | Drafted the chunkers, stats, BM25, RRF fusion, reranker, claim check, retrieval and answer metrics, the eval runner, gate tuning and the report fillers with their tests; ran the real-corpus builds and the dev evals (PRs #17 to #21, #23, #24, #26, #44, #57, #59, #62, #74) | Sai reviewed the formulas, tokenizer and fail-closed rules, ran the real models locally, checked every artifact number and reviewed the results tables |
| LLM client, prompt, generation, answer pipeline, server, query log, chat page, report 05 (Pavan) | Claude Code | Drafted the Claude API and Bedrock client, the prompts, `generate()`, `answer()`, the SSE server, the query log, the frontend and the report 05 tooling with their tests (PRs #30, #51 to #55, #58, #63, #64, #68) | Pavan reviewed the prompt rules and the fail-closed order, ran the app end to end on the real index and compared the UI with the spec |
| Safety router, dev/test split, PII tests, injection page and harness, verification tooling, reports 03 and 07, demo script (Dorai) | Claude Code | Drafted the router rules and referral wording, the split code, the extra PII tests, the planted injection page and its harness, the golden checker and item viewer, and the reports from the artifacts (PRs #15, #42, #43, #45, #47, #61, #65, #70) | Dorai set the rules and referral text, reviewed every tag against the golden questions, and reviewed the report wording; no AI tool wrote or edited a golden item |
| Check behavior fixes and dev reruns (Nayan) | Claude Code | Found the failing cases in the eval traces and drafted the fixes and tests for sentence dropping, judge strictness, follow-up rewriting and judge parsing; reran the dev evals (PRs #71 to #73, #75, #76) | Nayan reviewed each change against the proposal, ran the suite and replayed the failing cases locally |
| Team playbook and plan updates (Pavan) | Claude Code | Drafted `CLAUDE.md`, the `/start` and `/ship` commands, `docs/DELIVERABLES.md`, the UI spec and the plan updates (PRs #11, #33, #37, #39, #46, #50, #63) | The team agreed the owners and timings in the group chat before each merge |
