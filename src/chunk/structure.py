"""Stage 3 (structure-aware): one chunk per section, headings carried into the text.

Chunks follow the document's own sections (`sections[].heading_path`), so a chunk
never mixes two topics. Each chunk's text starts with its heading path, e.g.
"Recreational Flyers > Fly at or below 400 feet", so the heading words are embedded
and searchable with the passage. A section that fits in 512 tokens (heading
included) is one chunk. A bigger one is split:

- the section text into windows of up to 512 tokens with 64 overlap, each with
  the heading in front;
- each table on its own, whole if it fits, otherwise by rows, with the header
  row repeated in every part so each part still reads as a table.

    make chunk STRATEGY=struct   ->   python -m src.chunk.structure
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from src.chunk.fixed import (
    Tokenize,
    bge_tokenizer,
    make_chunk,
    read_normalized,
    word_windows,
    write_chunks,
)
from src.common import paths
from src.common.config import load_config
from src.common.contracts import Chunk, NormalizedDoc, Section, strategy_tag, validate_chunk

STRATEGY = "struct"
VERSION = "v1"
HEADING_SEP = " > "


def heading_prefix(heading_path: list[str], tokenize: Tokenize, max_tokens: int) -> str:
    """'A > B' plus a blank line. A very long path keeps only its last heading, so the
    prefix never takes more than a quarter of the chunk."""
    for path in (heading_path, heading_path[-1:]):
        prefix = HEADING_SEP.join(h.strip() for h in path if h.strip())
        prefix = prefix + "\n\n" if prefix else ""
        if len(tokenize(prefix)) <= max_tokens // 4:
            return prefix
    return ""


def split_text(text: str, tokenize: Tokenize, budget: int, overlap: int) -> list[str]:
    """Pieces of `text` of at most `budget` tokens, overlapping, sliced from the original."""
    spans = tokenize(text)
    overlap = min(overlap, budget // 2)
    return [text[spans[s][0] : spans[e - 1][1]] for s, e in word_windows(spans, budget, overlap)]


def split_table(table: str, tokenize: Tokenize, budget: int, overlap: int) -> list[str]:
    """A Markdown table in parts of at most `budget` tokens, each repeating the header.

    Rows are packed greedily and never cut, except a single row too big for a part
    on its own, which falls back to token windows.
    """
    lines = table.strip().splitlines()
    is_table = len(lines) >= 3 and set(lines[1].replace("|", "").strip()) <= set("-: ")
    if not is_table:
        return split_text(table, tokenize, budget, overlap)
    header = "\n".join(lines[:2])
    room = budget - len(tokenize(header + "\n"))
    if room <= 0:
        return split_text(table, tokenize, budget, overlap)

    parts: list[str] = []
    rows: list[str] = []
    used = 0
    for row in lines[2:]:
        cost = len(tokenize(row + "\n"))
        if cost > room:  # one row bigger than a whole part
            if rows:
                parts.append("\n".join([header, *rows]))
                rows, used = [], 0
            parts += split_text(row, tokenize, budget, overlap)
            continue
        if rows and used + cost > room:
            parts.append("\n".join([header, *rows]))
            rows, used = [], 0
        rows.append(row)
        used += cost
    if rows:
        parts.append("\n".join([header, *rows]))
    return parts


def section_texts(section: Section, tokenize: Tokenize, max_tokens: int, overlap: int) -> list[str]:
    """The chunk texts for one section, each starting with the heading path."""
    prefix = heading_prefix(section["heading_path"], tokenize, max_tokens)
    pieces = [p.strip() for p in (section["text"], *section["tables"]) if p.strip()]
    if not pieces:
        return []
    whole = prefix + "\n\n".join(pieces)
    if len(tokenize(whole)) <= max_tokens:
        return [whole]

    budget = max_tokens - len(tokenize(prefix))
    body: list[str] = []
    if section["text"].strip():
        body += split_text(section["text"].strip(), tokenize, budget, overlap)
    for table in section["tables"]:
        if not table.strip():
            continue
        if len(tokenize(prefix + table.strip())) <= max_tokens:
            body.append(table.strip())
        else:
            body += split_table(table, tokenize, budget, overlap)
    return [prefix + piece for piece in body]


def chunk_doc(
    doc: NormalizedDoc, tokenize: Tokenize, max_tokens: int = 512, overlap: int = 64
) -> list[Chunk]:
    """Structure-aware chunks for one document, numbered in reading order."""
    chunks: list[Chunk] = []
    for section in doc["sections"]:
        for text in section_texts(section, tokenize, max_tokens, overlap):
            chunks.append(
                make_chunk(
                    doc,
                    STRATEGY,
                    len(chunks),
                    text,
                    list(section["heading_path"]),
                    len(tokenize(text)),
                )
            )
    return chunks


def chunk_corpus(
    docs: Iterable[NormalizedDoc], tokenize: Tokenize, max_tokens: int = 512, overlap: int = 64
) -> list[Chunk]:
    chunks = [c for doc in docs for c in chunk_doc(doc, tokenize, max_tokens, overlap)]
    errors = [e for c in chunks for e in validate_chunk(c)]
    if errors:
        raise ValueError(f"{len(errors)} invalid chunk(s), first: {errors[0]}")
    return chunks


def main(argv: Iterable[str] | None = None, tokenize: Tokenize | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cut normalized docs into section chunks.")
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
