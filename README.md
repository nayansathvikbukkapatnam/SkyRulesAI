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
cp .env.example .env   # fill in AWS_REGION, AWS_PROFILE, BEDROCK_MODEL_ID, CRAWL_CONTACT_EMAIL
make test              # unit tests run on tests/fixtures/, no network or AWS needed
make help              # every pipeline command
```

Dependencies: edit `requirements.in`, then run `make freeze` to re-pin `requirements.txt`.
