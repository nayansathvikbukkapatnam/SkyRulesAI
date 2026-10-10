"""The verification assignments must cover every golden item exactly once, and the
viewer must be read-only. Uses the real eval/golden.jsonl (ids only)."""

import json

import pytest

from src.common import paths
from src.eval import show_item

GOLDEN = paths.GOLDEN
pytestmark = pytest.mark.skipif(not GOLDEN.exists(), reason="no eval/golden.jsonl yet")


def golden_items():
    return [json.loads(x) for x in GOLDEN.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_every_item_is_assigned_exactly_once():
    assigned = show_item.load_assignments()
    flat = [i for ids in assigned.values() for i in ids]
    assert sorted(flat) == sorted(i["id"] for i in golden_items())
    assert len(flat) == len(set(flat))


def test_nobody_verifies_their_own_items():
    authors = {i["author"].casefold() for i in golden_items()}
    assert not authors & {name.casefold() for name in show_item.load_assignments()}


def test_load_is_balanced():
    sizes = sorted(len(v) for v in show_item.load_assignments().values())
    assert sizes[-1] - sizes[0] <= 1


def test_viewer_prints_the_item_and_never_writes(capsys):
    before = GOLDEN.read_bytes()
    first = next(iter(show_item.load_assignments().values()))[0]
    assert show_item.main([first]) == 0
    out = capsys.readouterr().out
    assert first in out and "QUESTION:" in out
    assert GOLDEN.read_bytes() == before


def test_viewer_rejects_unknown_ids_and_names(capsys):
    assert show_item.main(["g999"]) == 1
    assert show_item.main(["--name", "Nobody"]) == 1
