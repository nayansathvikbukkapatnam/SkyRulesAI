"""Stage 5: the Chroma vector store (a folder on disk, no server).

One collection, `skyrules`, holds the chunks of every strategy. Each chunk's
metadata carries `strategy` as the full tag (e.g. "fixed@v1"), so a query or a
delete can target one strategy, as the re-chunk migration needs.

    make index STRATEGY=struct   ->   python -m src.index.store --strategy struct
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import chromadb
import numpy as np
from chromadb.config import Settings

from src.common import paths
from src.common.config import load_config
from src.common.contracts import Candidate, Chunk, strategy_tag, validate_chunk
from src.index.embed import Embedder, model_revision

DISTANCE = "cosine"  # Chroma's default is "l2"; always set and check this explicitly


# --- Collection -------------------------------------------------------------


def get_client(path: Path | None = None) -> chromadb.ClientAPI:
    path = path or paths.CHROMA
    path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False))


def open_collection(client: chromadb.ClientAPI, name: str = "skyrules") -> chromadb.Collection:
    """Get or create the collection with cosine distance, then verify it really is cosine."""
    collection = client.get_or_create_collection(
        name,
        configuration={"hnsw": {"space": DISTANCE}},
        # Never let Chroma embed with its own default model: we pass our vectors.
        embedding_function=None,
    )
    assert_cosine(collection)
    return collection


def collection_space(collection: chromadb.Collection) -> str:
    return (collection.configuration.get("hnsw") or {}).get("space", "l2")


def assert_cosine(collection: chromadb.Collection) -> None:
    space = collection_space(collection)
    if space != DISTANCE:
        raise RuntimeError(
            f"collection {collection.name!r} uses {space!r} distance, expected {DISTANCE!r}. "
            "Delete chroma/ and re-run make index."
        )


# --- Filters ----------------------------------------------------------------


def build_where(
    strategy: str | None = None, page_type: str | Sequence[str] | None = None
) -> dict | None:
    """Chroma `where` filter for a strategy tag and/or one or more page types."""
    clauses: list[dict] = []
    if strategy:
        clauses.append({"strategy": strategy})
    if page_type:
        if isinstance(page_type, str):
            clauses.append({"page_type": page_type})
        else:
            clauses.append({"page_type": {"$in": list(page_type)}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


# --- Store ------------------------------------------------------------------


def to_metadata(chunk: Chunk) -> dict[str, Any]:
    return {
        "doc_id": chunk["doc_id"],
        "strategy": strategy_tag(chunk["strategy"], chunk["version"]),
        "ordinal": chunk["ordinal"],
        "page_type": chunk["page_type"],
        "source_url": chunk["source_url"],
        "title": chunk["title"],
        "heading_path": " > ".join(chunk["heading_path"]),
        "effective_date": chunk["effective_date"],
        "n_tokens": chunk["n_tokens"],
        "chunk_hash": chunk["chunk_hash"],
    }


class ChunkStore:
    def __init__(self, collection: chromadb.Collection, embedder: Embedder) -> None:
        assert_cosine(collection)
        self.collection = collection
        self.embedder = embedder

    @classmethod
    def open(cls, cfg: dict, path: Path | None = None) -> ChunkStore:
        collection = open_collection(get_client(path), cfg["vector_db"]["collection"])
        return cls(collection, Embedder.from_config(cfg))

    def upsert(self, chunks: Sequence[Chunk], batch_size: int = 512) -> int:
        """Embed and upsert by chunk_id. Re-upserting the same chunks changes nothing."""
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors = self.embedder.embed([c["text"] for c in batch])
            self.collection.upsert(
                ids=[c["chunk_id"] for c in batch],
                embeddings=vectors,
                documents=[c["text"] for c in batch],
                metadatas=[to_metadata(c) for c in batch],
            )
        return len(chunks)

    def ids(self, where: dict | None = None) -> set[str]:
        return set(self.collection.get(where=where, include=[])["ids"])

    def count(self, where: dict | None = None) -> int:
        return self.collection.count() if where is None else len(self.ids(where))

    def delete(self, where: dict) -> int:
        """Delete every chunk matching `where`, e.g. {"strategy": "fixed@v1"}. Returns the count."""
        if not where:
            raise ValueError("refusing to delete without a filter")
        doomed = self.ids(where)
        if doomed:
            self.collection.delete(ids=sorted(doomed))
        return len(doomed)

    def sync(self, chunks: Sequence[Chunk], tag: str) -> dict[str, int]:
        """Make the index hold exactly these chunks for one strategy.

        Upserts every chunk, then deletes chunks of the same strategy that are
        no longer in the file (e.g. a document that now yields fewer chunks).
        """
        wanted = {c["chunk_id"] for c in chunks}
        self.upsert(chunks)
        stale = self.ids({"strategy": tag}) - wanted
        if stale:
            self.collection.delete(ids=sorted(stale))
        return {"upserted": len(chunks), "stale_removed": len(stale), "total": self.count()}

    def query(
        self,
        text: str,
        k: int = 20,
        strategy: str | None = None,
        page_type: str | Sequence[str] | None = None,
    ) -> list[Candidate]:
        """Dense search. dense_score is cosine similarity (1 - cosine distance)."""
        result = self.collection.query(
            query_embeddings=[self.embedder.embed_query(text)],
            n_results=k,
            where=build_where(strategy, page_type),
            include=["documents", "metadatas", "distances"],
        )
        return [
            Candidate(
                chunk_id=cid,
                text=doc,
                source_url=meta["source_url"],
                title=meta["title"],
                effective_date=meta.get("effective_date"),
                page_type=meta["page_type"],
                dense_score=1.0 - float(dist),
                bm25_score=None,
                fused_score=None,
            )
            for cid, doc, meta, dist in zip(
                result["ids"][0],
                result["documents"][0],
                result["metadatas"][0],
                result["distances"][0],
                strict=True,
            )
        ]


# --- make index -------------------------------------------------------------


def read_chunks(path: Path) -> list[Chunk]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run make chunk STRATEGY=... first")
    chunks = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    errors = [e for c in chunks for e in validate_chunk(c)]
    if errors:
        raise ValueError(f"{len(errors)} invalid chunk(s) in {path}, first: {errors[0]}")
    return chunks


def embedding_info(store: ChunkStore, vectors_sample: np.ndarray) -> dict[str, Any]:
    from importlib.metadata import version

    norms = np.linalg.norm(vectors_sample, axis=1) if len(vectors_sample) else np.array([])
    return {
        "model": store.embedder.model_name,
        "hf_revision": model_revision(store.embedder.model_name),
        "dimension": store.embedder.dimension,
        "distance": collection_space(store.collection),
        "normalized": True,
        "query_instruction": store.embedder.query_instruction,
        "sample_norm_min": float(norms.min()) if norms.size else None,
        "sample_norm_max": float(norms.max()) if norms.size else None,
        "sentence_transformers": version("sentence-transformers"),
        "chromadb": version("chromadb"),
        "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _bm25_hook(tag: str) -> str:
    """Build BM25 from the same chunk file once Sai's src/retrieve/bm25.py (S4) provides it."""
    try:
        from src.retrieve.bm25 import build_bm25  # type: ignore[attr-defined]
    except ImportError:
        return "skipped (src.retrieve.bm25.build_bm25 not merged yet)"
    build_bm25(tag)
    return "built"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Embed a chunk file into Chroma.")
    parser.add_argument("--strategy", required=True, help="fixed | struct | fixed@v1 | struct@v1")
    parser.add_argument("--config", default="default")
    args = parser.parse_args(list(argv) if argv is not None else None)

    tag = args.strategy if "@" in args.strategy else f"{args.strategy}@v1"
    cfg = load_config(args.config)
    chunks = read_chunks(paths.chunk_file(tag))
    store = ChunkStore.open(cfg)

    started = time.perf_counter()
    stats = store.sync(chunks, tag)
    seconds = time.perf_counter() - started

    sample = store.embedder.embed([c["text"] for c in chunks[:50]])
    info = embedding_info(store, sample) | {"strategy": tag, **stats, "seconds": round(seconds, 1)}
    paths.ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = paths.ARTIFACTS / "embedding_info.json"
    out.write_text(json.dumps(info, indent=2) + "\n")

    print(
        f"{tag}: upserted {stats['upserted']}, removed {stats['stale_removed']} stale, "
        f"collection now {stats['total']} chunks ({seconds:.1f}s)"
    )
    print(f"BM25: {_bm25_hook(tag)}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
