.PHONY: setup crawl extract chunk index retrieve generate eval test clean

setup:
	@echo "TODO: create venv and install requirements"

crawl:
	@echo "TODO: crawl source documents into data/raw/"

extract:
	@echo "TODO: extract text from raw documents"

chunk:
	@echo "TODO: chunk extracted text"

index:
	@echo "TODO: embed chunks and build the Chroma index"

retrieve:
	@echo "TODO: run retrieval for a query"

generate:
	@echo "TODO: generate an answer with citations"

eval:
	@echo "TODO: run evaluation against the golden set"

test:
	@echo "TODO: run pytest"

clean:
	@echo "TODO: remove generated artifacts"
