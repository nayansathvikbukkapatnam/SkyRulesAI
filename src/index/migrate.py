"""Stage 6: re-chunk the index from saved text, without re-crawling and without orphans.

    make migrate FROM=fixed@v1 TO=struct@v1   ->   python -m src.index.migrate --from ... --to ...

Steps (proposal section 6.3):
1. Load data/normalized/. The crawler is never imported or called.
2. Generate the new strategy's chunks; write the chunk file and a manifest of every
   expected chunk_id.
3. Delete the old strategy's chunks from Chroma by metadata filter (plus any stale
   chunks of the new strategy that are not in the manifest).
4. Upsert the new chunks by their deterministic IDs; rebuild BM25 from the new file.
5. Orphan check: (index IDs - manifest IDs) and (manifest IDs - index IDs) must both
   be empty, and no chunk with the old strategy tag may remain. Exit 1 otherwise.

The run is written to reports/artifacts/migration_log.txt for 02_chunking.md.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from src.chunk import fixed, structure
from src.chunk.fixed import Tokenize, bge_tokenizer, read_normalized, write_chunks
from src.common import paths
from src.common.config import load_config
from src.common.contracts import STRATEGIES, Chunk, NormalizedDoc
from src.index.store import ChunkStore, build_bm25_if_available

ChunkCorpus = Callable[[Iterable[NormalizedDoc], Tokenize, int, int], list[Chunk]]

CHUNKERS: dict[str, tuple[ChunkCorpus, str]] = {
    "fixed": (fixed.chunk_corpus, fixed.VERSION),
    "struct": (structure.chunk_corpus, structure.VERSION),
}

MAX_IDS_IN_LOG = 20


def parse_tag(tag: str) -> tuple[str, str]:
    """'struct@v1' -> ('struct', 'v1'), checked against the chunkers we have."""
    strategy, sep, version = tag.partition("@")
    if not sep or strategy not in STRATEGIES:
        raise ValueError(f"not a strategy tag: {tag!r} (expected e.g. fixed@v1, struct@v1)")
    if version != CHUNKERS[strategy][1]:
        raise ValueError(
            f"{tag!r}: the {strategy} chunker produces version {CHUNKERS[strategy][1]}"
        )
    return strategy, version


@dataclass
class OrphanCheck:
    index_minus_manifest: list[str]
    manifest_minus_index: list[str]
    old_strategy_count: int

    @property
    def passed(self) -> bool:
        return (
            not self.index_minus_manifest
            and not self.manifest_minus_index
            and self.old_strategy_count == 0
        )


def orphan_check(store: ChunkStore, manifest_ids: set[str], old_tag: str) -> OrphanCheck:
    index_ids = store.ids()
    return OrphanCheck(
        index_minus_manifest=sorted(index_ids - manifest_ids),
        manifest_minus_index=sorted(manifest_ids - index_ids),
        old_strategy_count=store.count({"strategy": old_tag}),
    )


def strategy_counts(store: ChunkStore) -> dict[str, int]:
    metadatas = store.collection.get(include=["metadatas"])["metadatas"] or []
    return dict(sorted(Counter(m.get("strategy", "?") for m in metadatas).items()))


@dataclass
class MigrationResult:
    from_tag: str
    to_tag: str
    n_docs: int
    before: dict[str, int]
    generated: int
    deleted_old: int
    deleted_stale_new: int
    upserted: int
    bm25: str
    check: OrphanCheck
    after: dict[str, int]
    seconds: float
    chunk_file: Path
    manifest_file: Path
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.check.passed


def write_manifest(chunks: list[Chunk], tag: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "strategy": tag,
        "count": len(chunks),
        "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "chunk_ids": [c["chunk_id"] for c in chunks],
    }
    out.write_text(json.dumps(manifest, indent=2) + "\n")


def manifest_path(tag: str) -> Path:
    return paths.CHUNKS / f"{tag}.manifest.json"


def migrate(
    store: ChunkStore,
    from_tag: str,
    to_tag: str,
    tokenize: Tokenize,
    max_tokens: int = 512,
    overlap: int = 64,
    normalized_dir: Path | None = None,
    chunk_out: Path | None = None,
    manifest_out: Path | None = None,
    build_bm25: Callable[[str], str] = build_bm25_if_available,
) -> MigrationResult:
    """Replace `from_tag` chunks in the index with freshly generated `to_tag` chunks."""
    if from_tag == to_tag:
        raise ValueError(f"FROM and TO are the same strategy: {from_tag}")
    parse_tag(from_tag)
    to_strategy, _ = parse_tag(to_tag)
    normalized_dir = normalized_dir or paths.NORMALIZED
    chunk_out = chunk_out or paths.chunk_file(to_tag)
    manifest_out = manifest_out or manifest_path(to_tag)
    started = time.perf_counter()
    warnings: list[str] = []

    # 1. Saved normalized text only: no crawl.
    docs = read_normalized(normalized_dir)
    before = strategy_counts(store)
    if before.get(from_tag, 0) == 0:
        warnings.append(f"no {from_tag} chunks were in the index before migrating")

    # 2. New chunks, chunk file and manifest.
    chunk_corpus, _ = CHUNKERS[to_strategy]
    chunks = chunk_corpus(docs, tokenize, max_tokens, overlap)
    manifest_ids = {c["chunk_id"] for c in chunks}
    if len(manifest_ids) != len(chunks):
        raise ValueError(f"{to_tag} produced duplicate chunk IDs; refusing to migrate")
    write_chunks(chunks, chunk_out)
    write_manifest(chunks, to_tag, manifest_out)

    # 3. Delete the old strategy, and new-strategy chunks the manifest no longer lists.
    deleted_old = store.delete({"strategy": from_tag})
    stale_new = store.ids({"strategy": to_tag}) - manifest_ids
    if stale_new:
        store.collection.delete(ids=sorted(stale_new))

    # 4. Upsert by deterministic ID; BM25 from the same chunk file.
    store.upsert(chunks)
    bm25 = build_bm25(to_tag)

    # 5. Orphan check.
    check = orphan_check(store, manifest_ids, from_tag)
    return MigrationResult(
        from_tag=from_tag,
        to_tag=to_tag,
        n_docs=len(docs),
        before=before,
        generated=len(chunks),
        deleted_old=deleted_old,
        deleted_stale_new=len(stale_new),
        upserted=len(chunks),
        bm25=bm25,
        check=check,
        after=strategy_counts(store),
        seconds=round(time.perf_counter() - started, 1),
        chunk_file=chunk_out,
        manifest_file=manifest_out,
        warnings=warnings,
    )


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=paths.ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(paths.ROOT))
    except ValueError:
        return str(path)


def _id_lines(label: str, ids: list[str]) -> list[str]:
    lines = [f"  {label}: {len(ids)}"]
    lines += [f"    {i}" for i in ids[:MAX_IDS_IN_LOG]]
    if len(ids) > MAX_IDS_IN_LOG:
        lines.append(f"    ... and {len(ids) - MAX_IDS_IN_LOG} more")
    return lines


def format_log(r: MigrationResult) -> str:
    ok = "PASS" if r.passed else "FAIL"
    c = r.check
    lines = [
        f"Re-chunk migration {r.from_tag} -> {r.to_tag}",
        f"run at {datetime.now(UTC).isoformat(timespec='seconds')}, commit {_git_commit()}",
        f"source: {r.n_docs} normalized documents from {_rel(paths.NORMALIZED)} (no re-crawl)",
        "",
        f"index before: {r.before or '{}'}",
        f"generated {r.generated} {r.to_tag} chunks -> {_rel(r.chunk_file)}",
        f"manifest: {_rel(r.manifest_file)} ({r.generated} chunk IDs)",
        f"deleted {r.deleted_old} {r.from_tag} chunks (metadata filter strategy == {r.from_tag})",
        f"deleted {r.deleted_stale_new} stale {r.to_tag} chunks not in the manifest",
        f"upserted {r.upserted} {r.to_tag} chunks",
        f"BM25: {r.bm25}",
        f"index after: {r.after or '{}'}",
        "",
        f"orphan check: {ok}",
        *_id_lines("index IDs - manifest IDs (must be 0)", c.index_minus_manifest),
        *_id_lines("manifest IDs - index IDs (must be 0)", c.manifest_minus_index),
        f"  chunks still tagged {r.from_tag} (must be 0): {c.old_strategy_count}",
        "",
        f"took {r.seconds}s",
    ]
    lines += [f"warning: {w}" for w in r.warnings]
    if r.passed:
        lines.append("next: re-run the dev evaluation on the new chunks and report the change")
    return "\n".join(lines) + "\n"


def run(
    argv: Iterable[str] | None = None,
    tokenize: Tokenize | None = None,
    build_bm25: Callable[[str], str] = build_bm25_if_available,
) -> int:
    parser = argparse.ArgumentParser(description="Re-chunk the index without re-crawling.")
    parser.add_argument("--from", dest="from_tag", required=True, help="e.g. fixed@v1")
    parser.add_argument("--to", dest="to_tag", required=True, help="e.g. struct@v1")
    parser.add_argument("--config", default="default")
    args = parser.parse_args(list(argv) if argv is not None else None)

    cfg = load_config(args.config)
    store = ChunkStore.open(cfg)
    result = migrate(
        store,
        args.from_tag,
        args.to_tag,
        tokenize or bge_tokenizer(cfg["embedding"]["model"]),
        cfg["chunking"]["max_tokens"],
        cfg["chunking"]["overlap_tokens"],
        build_bm25=build_bm25,
    )
    log = format_log(result)
    paths.ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = paths.ARTIFACTS / "migration_log.txt"
    out.write_text(log)
    data = paths.ARTIFACTS / "migration.json"
    data.write_text(json.dumps(result_record(result), indent=2) + "\n")
    print(log, end="")
    print(f"wrote {_rel(out)} and {_rel(data)}")
    return 0 if result.passed else 1


def result_record(r: MigrationResult) -> dict:
    """The run as JSON, so report 02's tables are filled from data, not from the text log."""
    return {
        "from": r.from_tag,
        "to": r.to_tag,
        "commit": _git_commit(),
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "n_docs": r.n_docs,
        "before": r.before,
        "generated": r.generated,
        "deleted_old": r.deleted_old,
        "deleted_stale_new": r.deleted_stale_new,
        "upserted": r.upserted,
        "after": r.after,
        "bm25": r.bm25,
        "orphan_check": {
            "passed": r.passed,
            "index_minus_manifest": len(r.check.index_minus_manifest),
            "manifest_minus_index": len(r.check.manifest_minus_index),
            "old_strategy_count": r.check.old_strategy_count,
        },
        "seconds": r.seconds,
    }


if __name__ == "__main__":
    sys.exit(run())
