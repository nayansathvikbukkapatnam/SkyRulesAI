# SkyRules AI: every reported number comes from one of these targets (CLAUDE.md section 6).
# Targets whose code has not been written yet stop with "not implemented yet".

PYTHON_311 ?= python3.11
PY := .venv/bin/python

STRATEGY ?= struct
CONFIG ?= b2
SPLIT ?= dev
FROM ?= fixed@v1
TO ?= struct@v1

CHUNK_MODULE_fixed := src.chunk.fixed
CHUNK_MODULE_struct := src.chunk.structure

# $(call run,<module>,<args>): run a module if its file exists, else fail clearly.
define run
	@if [ ! -x $(PY) ]; then echo "no .venv yet: run 'make setup' first"; exit 1; fi
	@if [ -f $(subst .,/,$(1)).py ]; then $(PY) -m $(1) $(2); \
	else echo "not implemented yet: $(1) (owner and branch in CLAUDE.md section 9)"; exit 1; fi
endef

.PHONY: help setup freeze fixtures crawl extract chunk index migrate golden-check split eval stats app test lint clean

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-14s %s\n", $$1, $$2}'

setup: ## create .venv (Python 3.11) and install pinned requirements
	@command -v $(PYTHON_311) >/dev/null || { echo "Python 3.11 not found (macOS: brew install python@3.11)"; exit 1; }
	$(PYTHON_311) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt
	@[ -f .env ] || echo "next: cp .env.example .env and fill in the values"

freeze: ## re-pin requirements.txt after editing requirements.in
	$(PY) -m pip install -r requirements.in
	@{ echo "# Pinned with: make freeze (pip freeze from a Python 3.11 venv built from requirements.in)."; \
	   echo "# Edit requirements.in, not this file."; \
	   $(PY) -m pip freeze --exclude-editable | sort -f; } > requirements.txt

fixtures: ## rebuild tests/fixtures from tests/fixtures/build_fixtures.py
	$(PY) -m tests.fixtures.build_fixtures

crawl: ## crawl faa.gov once into data/raw/ (FORCE=1 to crawl again)
	$(call run,src.crawl.crawler,$(if $(FORCE),--force))

extract: ## data/raw/ -> data/normalized/ + reports/artifacts/ingestion_stats.json
	$(call run,src.extract.normalize,)

chunk: ## data/normalized/ -> data/chunks/ (STRATEGY=fixed|struct)
	$(call run,$(CHUNK_MODULE_$(STRATEGY)),)

index: ## embed chunks into Chroma and build BM25 (STRATEGY=fixed|struct)
	$(call run,src.index.store,--strategy $(STRATEGY))

migrate: ## re-chunk migration with orphan check (FROM=fixed@v1 TO=struct@v1)
	$(call run,src.index.migrate,--from $(FROM) --to $(TO))

golden-check: ## validate eval/golden.jsonl (schema, quotas, quotes, verifier; NO_QUOTES=1 before extraction)
	$(call run,src.eval.golden_check,$(if $(NO_QUOTES),--no-quotes))

split: ## stratified 60/40 dev/test split, seed 42 (refuses to overwrite)
	$(call run,src.eval.split,)

eval: ## run a config on a split (CONFIG=b2 SPLIT=dev; the test split needs FINAL=1)
	$(call run,src.eval.run_eval,--config $(CONFIG) --split $(SPLIT) $(if $(FINAL),--final))

stats: ## bootstrap intervals and McNemar tests from saved eval runs
	$(call run,src.eval.stats,)

app: ## start the app at http://localhost:8000
	@if [ -f src/app/server.py ]; then $(PY) -m uvicorn src.app.server:app --port 8000; \
	else echo "not implemented yet: src.app.server (owner and branch in CLAUDE.md section 9)"; exit 1; fi

test: ## run the unit tests
	$(PY) -m pytest

lint: ## run ruff
	$(PY) -m ruff check .

clean: ## remove caches (never touches data/ or chroma/)
	rm -rf .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
