"""Stage 7 (keyword side): a BM25Okapi index over the same chunk file Chroma holds.

Tokens are lowercase alphanumeric runs, so "Part 107", "part-107" and "PART 107"
all become ["part", "107"]. Stopwords are dropped and plurals are reduced to their
singular ("flyers" -> "flyer", "rules" -> "rule"), so a question like "How high can
I fly as a hobby flyer?" is matched on "high", "fly", "hobby", "flyer" instead of
on "how", "can", "i", "as", "a". The index is pickled to data/index/bm25_<tag>.pkl
with the chunk fields a Candidate needs, so a search never re-reads the chunk file.
TOKENIZER_VERSION is stored in the pickle: a pickle built with an older tokenizer
is refused on load, because its tokens would no longer match the query's.

    make index STRATEGY=struct      (store.py calls build_bm25 after embedding)
    python -m src.retrieve.bm25 --strategy struct
"""

from __future__ import annotations

import argparse
import pickle
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

from src.common import paths
from src.common.contracts import Candidate, Chunk

_TOKEN = re.compile(r"[a-z0-9]+")
_KEEP = ("chunk_id", "text", "source_url", "title", "effective_date", "page_type")

# Bump when tokenize() changes: every BM25 pickle must then be rebuilt (make index).
TOKENIZER_VERSION = 2

# Function words that carry no topic. Kept short on purpose: "not", "no", "under",
# "over", "must" and "may" stay, because they change what a rule says.
STOPWORDS = frozenset(
    "a an and are as at be by can do does for from has have how i if in is it its "
    "me my of on or that the this to was what when where which who will with you your".split()
)


def _singular(token: str) -> str:
    """A light plural reduction: flyers -> flyer, batteries -> battery, classes -> class.

    Digits and short tokens are left alone, and so are words ending in -ss, -us or -is
    (class, status, analysis). It is applied to chunks and queries alike, so a
    mismatch cannot arise; it is not a real stemmer.
    """
    if len(token) <= 3 or token.isdigit():
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("sses", "xes", "ches", "shes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    return [
        t for t in (_singular(raw) for raw in _TOKEN.findall(text.lower())) if t not in STOPWORDS
    ]


class BM25Index:
    """BM25Okapi plus the chunk fields needed to return Candidates."""

    def __init__(self, tag: str, chunks: Sequence[Chunk]) -> None:
        if not chunks:
            raise ValueError(f"no chunks to index for {tag}")
        self.tag = tag
        self.tokenizer_version = TOKENIZER_VERSION
        self.docs = [{k: c[k] for k in _KEEP} for c in chunks]
        self.bm25 = BM25Okapi([tokenize(c["text"]) for c in chunks])

    def __len__(self) -> int:
        return len(self.docs)

    def search(
        self, query: str, k: int = 20, page_type: str | Sequence[str] | None = None
    ) -> list[Candidate]:
        """Top k chunks by BM25 score. Chunks sharing no term with the query
        (score 0 or below) are left out: they are not keyword matches."""
        terms = tokenize(query)
        if not terms:
            return []
        scores = np.asarray(self.bm25.get_scores(terms), dtype=float)
        if page_type:
            allowed = {page_type} if isinstance(page_type, str) else set(page_type)
            mask = np.array([d["page_type"] in allowed for d in self.docs])
            scores = np.where(mask, scores, -np.inf)
        # Stable sort on -score: ties keep chunk-file order, so results are reproducible.
        order = np.argsort(-scores, kind="stable")[:k]
        return [
            Candidate(
                **self.docs[i], dense_score=None, bm25_score=float(scores[i]), fused_score=None
            )
            for i in order
            if scores[i] > 0
        ]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            pickle.dump(self, f)

    @classmethod
    def load(cls, tag: str, path: Path | None = None) -> BM25Index:
        path = path or paths.bm25_path(tag)
        if not path.exists():
            raise FileNotFoundError(f"{path} not found; run make index STRATEGY=... first")
        # Only ever our own file, written by build_bm25 from our chunk file.
        with path.open("rb") as f:
            index = pickle.load(f)
        if not isinstance(index, cls) or index.tag != tag:
            raise ValueError(f"{path} does not hold the BM25 index for {tag}")
        built_with = getattr(index, "tokenizer_version", 1)
        if built_with != TOKENIZER_VERSION:
            raise ValueError(
                f"{path} was built with tokenizer v{built_with}, but the code is v"
                f"{TOKENIZER_VERSION}; rebuild it with make index STRATEGY={tag.split('@')[0]}"
            )
        return index


def build_bm25(tag: str, chunks_path: Path | None = None, out: Path | None = None) -> BM25Index:
    """Build the index from data/chunks/<tag>.jsonl and save it to data/index/."""
    from src.index.store import read_chunks  # validates every chunk

    index = BM25Index(tag, read_chunks(chunks_path or paths.chunk_file(tag)))
    index.save(out or paths.bm25_path(tag))
    return index


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the BM25 index for a chunk file.")
    parser.add_argument("--strategy", required=True, help="fixed | struct | fixed@v1 | struct@v1")
    args = parser.parse_args(list(argv) if argv is not None else None)

    tag = args.strategy if "@" in args.strategy else f"{args.strategy}@v1"
    index = build_bm25(tag)
    print(f"{tag}: BM25 over {len(index)} chunks, wrote {paths.bm25_path(tag)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
