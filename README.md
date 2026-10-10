# SkyRulesAI

A retrieval-augmented assistant that answers questions about aviation rules and regulations, with citations to the source text.

## Team

| Name | GitHub | Role |
|------|--------|------|
| Nayan Sathvik | @nayansathvikbukkapatnam | TBD |
| Pavan | @pavan2564 | TBD |
| Dorai Saketh Guntur | @sakethguntur | TBD |
| Adinath Kadam | @AdinathKadam | TBD |
| Sai Praneeth | @saipraneeth112698 | TBD |

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
