"""Stage 6: re-chunk from saved text, delete the old strategy, and prove no orphans remain."""

import json
import re
import subprocess
import sys

import pytest

from src.chunk import structure
from src.common import paths
from src.index import migrate as mig
from src.index.embed import Embedder
from src.index.store import ChunkStore, get_client, open_collection
from tests.test_index import FakeModel

FIX = paths.FIXTURES


def word_spans(text):
    """Stands in for the bge tokenizer: one token per whitespace-separated word."""
    return [m.span() for m in re.finditer(r"\S+", text)]


def fixture_chunks(tag):
    return [json.loads(x) for x in (FIX / "chunks" / f"{tag}.jsonl").read_text().splitlines()]


def no_bm25(tag):
    return "skipped (test)"


@pytest.fixture
def store(tmp_path):
    collection = open_collection(get_client(tmp_path / "chroma"), "skyrules")
    return ChunkStore(collection, Embedder("fake", model=FakeModel()))


@pytest.fixture
def run(store, tmp_path):
    def _run(from_tag="fixed@v1", to_tag="struct@v1", build_bm25=no_bm25):
        return mig.migrate(
            store,
            from_tag,
            to_tag,
            word_spans,
            normalized_dir=FIX / "normalized",
            chunk_out=tmp_path / "chunks" / f"{to_tag}.jsonl",
            manifest_out=tmp_path / "chunks" / f"{to_tag}.manifest.json",
            build_bm25=build_bm25,
        )

    return _run


def test_fixed_to_struct_leaves_only_the_new_chunks(store, run):
    store.upsert(fixture_chunks("fixed@v1"))
    result = run()

    expected = structure.chunk_corpus(
        [json.loads(p.read_text()) for p in sorted((FIX / "normalized").glob("*.json"))],
        word_spans,
    )
    assert result.passed
    assert result.before == {"fixed@v1": 5}
    assert result.deleted_old == 5
    assert result.after == {"struct@v1": len(expected)}
    assert store.ids() == {c["chunk_id"] for c in expected}
    assert result.check.index_minus_manifest == []
    assert result.check.manifest_minus_index == []
    assert result.check.old_strategy_count == 0


def test_chunk_file_and_manifest_list_exactly_what_is_indexed(store, run):
    store.upsert(fixture_chunks("fixed@v1"))
    result = run()
    manifest = json.loads(result.manifest_file.read_text())
    written = [json.loads(x) for x in result.chunk_file.read_text().splitlines()]
    assert manifest["strategy"] == "struct@v1"
    assert manifest["count"] == len(written) == len(manifest["chunk_ids"])
    assert set(manifest["chunk_ids"]) == {c["chunk_id"] for c in written} == store.ids()


def test_running_the_migration_twice_changes_nothing(store, run):
    store.upsert(fixture_chunks("fixed@v1"))
    run()
    ids_after_first = store.ids()
    second = run()
    assert second.passed
    assert second.deleted_old == 0
    assert store.ids() == ids_after_first
    assert "no fixed@v1 chunks were in the index before migrating" in second.warnings


def test_stale_chunks_of_the_new_strategy_are_removed(store, run):
    store.upsert(fixture_chunks("fixed@v1"))
    stale = dict(fixture_chunks("struct@v1")[0])
    stale["ordinal"] = 99
    stale["chunk_id"] = stale["chunk_id"].rsplit("::", 1)[0] + "::0099"
    store.upsert([stale])

    result = run()
    assert result.passed
    assert result.deleted_stale_new == 1
    assert stale["chunk_id"] not in store.ids()


def test_reverse_migration_struct_to_fixed_also_passes(store, run):
    store.upsert(fixture_chunks("struct@v1"))
    result = run("struct@v1", "fixed@v1")
    assert result.passed
    assert set(result.after) == {"fixed@v1"}


# --- The orphan check must catch each kind of leftover -------------------------


def test_check_catches_an_index_chunk_missing_from_the_manifest(store, run):
    store.upsert(fixture_chunks("fixed@v1"))
    orphan = dict(fixture_chunks("struct@v1")[0])
    orphan["chunk_id"] = orphan["chunk_id"].rsplit("::", 1)[0] + "::0042"

    # A BM25 step that sneaks a chunk in after the stale cleanup simulates corruption.
    result = run(build_bm25=lambda tag: store.upsert([orphan]) and "built")
    assert not result.passed
    assert result.check.index_minus_manifest == [orphan["chunk_id"]]


def test_check_catches_a_manifest_chunk_missing_from_the_index(store, run):
    store.upsert(fixture_chunks("fixed@v1"))
    result = run()
    victim = sorted(store.ids())[0]
    store.collection.delete(ids=[victim])
    check = mig.orphan_check(
        store, set(json.loads(result.manifest_file.read_text())["chunk_ids"]), "fixed@v1"
    )
    assert not check.passed
    assert check.manifest_minus_index == [victim]


def test_check_catches_a_leftover_old_strategy_chunk(store, run):
    store.upsert(fixture_chunks("fixed@v1"))
    result = run()
    leftover = fixture_chunks("fixed@v1")[0]
    store.upsert([leftover])
    manifest = set(json.loads(result.manifest_file.read_text())["chunk_ids"])
    check = mig.orphan_check(store, manifest, "fixed@v1")
    assert not check.passed
    assert check.old_strategy_count == 1
    assert check.index_minus_manifest == [leftover["chunk_id"]]


# --- Inputs, CLI and log --------------------------------------------------------


@pytest.mark.parametrize(
    "from_tag,to_tag",
    [
        ("fixed@v1", "fixed@v1"),
        ("fixed@v1", "semantic@v1"),
        ("fixed", "struct@v1"),
        ("fixed@v1", "struct@v9"),
    ],
)
def test_bad_strategy_tags_are_refused(run, from_tag, to_tag):
    with pytest.raises(ValueError):
        run(from_tag, to_tag)


def test_cli_writes_the_log_and_exits_zero(store, tmp_path, monkeypatch):
    store.upsert(fixture_chunks("fixed@v1"))
    monkeypatch.setattr(paths, "NORMALIZED", FIX / "normalized")
    monkeypatch.setattr(paths, "CHUNKS", tmp_path / "chunks")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(mig.ChunkStore, "open", classmethod(lambda cls, cfg, path=None: store))

    code = mig.run(
        ["--from", "fixed@v1", "--to", "struct@v1"], tokenize=word_spans, build_bm25=no_bm25
    )
    log = (tmp_path / "artifacts" / "migration_log.txt").read_text()
    assert code == 0
    assert "orphan check: PASS" in log
    assert "deleted 5 fixed@v1 chunks" in log
    assert "(no re-crawl)" in log
    assert (tmp_path / "chunks" / "struct@v1.jsonl").exists()
    assert (tmp_path / "chunks" / "struct@v1.manifest.json").exists()
    record = json.loads((tmp_path / "artifacts" / "migration.json").read_text())
    assert record["orphan_check"] == {
        "passed": True,
        "index_minus_manifest": 0,
        "manifest_minus_index": 0,
        "old_strategy_count": 0,
    }
    assert (record["from"], record["to"], record["deleted_old"]) == ("fixed@v1", "struct@v1", 5)
    assert record["before"] == {"fixed@v1": 5}


def test_failed_check_is_logged_with_the_mismatched_ids():
    result = mig.MigrationResult(
        "fixed@v1", "struct@v1", 5, {"fixed@v1": 5}, 9, 5, 0, 9, "skipped",
        mig.OrphanCheck(["a1b2c3d4e5f6::struct@v1::0042"], [], 1),
        {"struct@v1": 10}, 0.1, paths.CHUNKS / "x.jsonl", paths.CHUNKS / "x.manifest.json",
    )  # fmt: skip
    log = mig.format_log(result)
    assert "orphan check: FAIL" in log
    assert "a1b2c3d4e5f6::struct@v1::0042" in log
    assert "next:" not in log


def test_migration_never_imports_the_crawler():
    code = (
        "import sys, src.index.migrate; print(any(m.startswith('src.crawl') for m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=paths.ROOT, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
