"""Stage 3 (baseline): fixed-size chunks of 512 bge tokens with 64 tokens of overlap.

Each normalized document's sections and tables are joined into one text, which is
cut into token windows with the bge-small tokenizer, so n_tokens is exact for the
embedding model. A chunk's text is the original substring covered by its tokens
(found through the tokenizer's character offsets), not decoded tokens, so case,
punctuation and spacing match the saved page and evidence quotes still match.

    make chunk STRATEGY=fixed   ->   python -m src.chunk.fixed
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from src.common import paths
from src.common.config import load_config
from src.common.contracts import (
    Chunk,
    NormalizedDoc,
    chunk_id_for,
    sha256_hex,
    strategy_tag,
    validate_chunk,
    validate_normalized_doc,
)

STRATEGY = "fixed"
VERSION = "v1"

# Character (start, end) offsets of each token in a text, without special tokens.
Tokenize = Callable[[str], list[tuple[int, int]]]


def bge_tokenizer(model_name: str = "BAAI/bge-small-en-v1.5") -> Tokenize:
    """The embedding model's own tokenizer, returning token character offsets."""
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    # Whole documents run past the model's 512 limit; we window them ourselves.
    tokenizer.model_max_length = sys.maxsize

    def tokenize(text: str) -> list[tuple[int, int]]:
        enc = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
        return [tuple(span) for span in enc["offset_mapping"]]

    return tokenize


def word_windows(
    spans: list[tuple[int, int]], max_tokens: int, overlap: int
) -> Iterator[tuple[int, int]]:
    """(start, end) token ranges of at most max_tokens, consecutive ones sharing about
    `overlap` tokens, each starting and ending on a word boundary.

    bge splits long words into pieces ("authorization" -> "authori", "##zation"). A
    window cut between two pieces would start a chunk with a word fragment, and the
    fragment re-tokenizes into more tokens than it had in place, so a 512-token
    window could come back as 513. A token starts a word when whitespace precedes
    it. Windows shrink to the last word start within max_tokens, and the next window
    starts at the first word start at or after `overlap` tokens back, so text is
    never skipped. Only a single word longer than a whole window is cut mid-word.
    """
    if not 0 <= overlap < max_tokens:
        raise ValueError(f"overlap must be in [0, max_tokens): {overlap=} {max_tokens=}")
    n = len(spans)
    starts_word = [i == 0 or spans[i][0] > spans[i - 1][1] for i in range(n)] + [True]
    start = 0
    while start < n:
        limit = min(start + max_tokens, n)
        end = limit
        while end > start and not starts_word[end]:
            end -= 1
        if end == start:  # one word longer than a whole window
            end = limit
        yield start, end
        if end == n:
            return
        nxt = max(end - overlap, start + 1)
        while nxt < end and not starts_word[nxt]:
            nxt += 1
        start = nxt


def doc_text(doc: NormalizedDoc) -> str:
    """A document's body: each section's text then its tables, separated by blank lines."""
    parts: list[str] = []
    for section in doc["sections"]:
        parts += [p for p in (section["text"], *section["tables"]) if p.strip()]
    return "\n\n".join(parts)


def make_chunk(
    doc: NormalizedDoc,
    strategy: str,
    ordinal: int,
    text: str,
    heading_path: list[str],
    n_tokens: int,
) -> Chunk:
    """One chunk in the section 5 contract (shared with the structure-aware chunker)."""
    return {
        "chunk_id": chunk_id_for(doc["doc_id"], strategy, VERSION, ordinal),
        "doc_id": doc["doc_id"],
        "strategy": strategy,
        "version": VERSION,
        "ordinal": ordinal,
        "text": text,
        "heading_path": heading_path,
        "page_type": doc["page_type"],
        "source_url": doc["source_url"],
        "title": doc["title"],
        "effective_date": doc["effective_date"],
        "n_tokens": n_tokens,
        "chunk_hash": sha256_hex(text),
    }


def chunk_doc(
    doc: NormalizedDoc, tokenize: Tokenize, max_tokens: int = 512, overlap: int = 64
) -> list[Chunk]:
    """Fixed-size chunks for one document. A fixed window can span sections, so
    heading_path is left empty (carrying headings is the struct@v1 strategy's job)."""
    text = doc_text(doc)
    spans = tokenize(text)
    chunks: list[Chunk] = []
    for start, end in word_windows(spans, max_tokens, overlap):
        piece = text[spans[start][0] : spans[end - 1][1]]
        chunks.append(make_chunk(doc, STRATEGY, len(chunks), piece, [], end - start))
    return chunks


def read_normalized(directory: Path) -> list[NormalizedDoc]:
    """Every data/normalized/*.json, sorted by doc_id so output order is reproducible."""
    files = sorted(directory.glob("*.json"))
    if not files:
        raise FileNotFoundError(f"no normalized documents in {directory}; run make extract first")
    docs = [json.loads(f.read_text()) for f in files]
    errors = [e for d in docs for e in validate_normalized_doc(d)]
    if errors:
        raise ValueError(f"{len(errors)} problem(s) in {directory}, first: {errors[0]}")
    return sorted(docs, key=lambda d: d["doc_id"])


def chunk_corpus(
    docs: Iterable[NormalizedDoc], tokenize: Tokenize, max_tokens: int = 512, overlap: int = 64
) -> list[Chunk]:
    chunks = [c for doc in docs for c in chunk_doc(doc, tokenize, max_tokens, overlap)]
    errors = [e for c in chunks for e in validate_chunk(c)]
    if errors:
        raise ValueError(f"{len(errors)} invalid chunk(s), first: {errors[0]}")
    return chunks


def write_chunks(chunks: list[Chunk], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks))


def main(argv: Iterable[str] | None = None, tokenize: Tokenize | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cut normalized docs into fixed-size chunks.")
    parser.add_argument("--input", type=Path, default=paths.NORMALIZED)
    parser.add_argument(
        "--output", type=Path, default=paths.chunk_file(strategy_tag(STRATEGY, VERSION))
    )
    parser.add_argument("--config", default="default")
    args = parser.parse_args(list(argv) if argv is not None else None)

    cfg: dict[str, Any] = load_config(args.config)
    settings = cfg["chunking"]
    docs = read_normalized(args.input)
    chunks = chunk_corpus(
        docs,
        tokenize or bge_tokenizer(cfg["embedding"]["model"]),
        settings["max_tokens"],
        settings["overlap_tokens"],
    )
    write_chunks(chunks, args.output)

    empty = len(docs) - len({c["doc_id"] for c in chunks})
    print(
        f"{strategy_tag(STRATEGY, VERSION)}: {len(chunks)} chunks from {len(docs)} docs"
        + (f" ({empty} empty docs skipped)" if empty else "")
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
