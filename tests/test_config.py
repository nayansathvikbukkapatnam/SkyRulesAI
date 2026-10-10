from itertools import pairwise

import pytest

from src.common import paths
from src.common.config import SWITCHES, load_config, switches, validate_config

ALL = ["b0", "b1", "b2", "a1", "a2", "a3", "b3"]

# The B2 -> B3 ablation adds one improvement per step. The gate and the answer
# checks are one step (A4 = B3), as in proposal section 9.4.
ABLATION = ["b2", "a1", "a2", "a3", "b3"]
STEP_GROUPS = [
    {"chunk_strategy"},
    {"retrieval_mode"},
    {"rerank"},
    {"gate", "gate_threshold", "checks"},
]


@pytest.mark.parametrize("name", ALL)
def test_every_config_loads(name, monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")
    cfg = load_config(name)
    assert set(SWITCHES) <= set(cfg)
    assert cfg["generator_model"] == "test-model"


def test_every_yaml_in_configs_is_tested():
    on_disk = {p.stem for p in paths.CONFIGS.glob("*.yaml")} - {"default"}
    assert on_disk == set(ALL)


def test_ablation_changes_one_thing_per_step(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")
    for (before, after), group in zip(pairwise(ABLATION), STEP_GROUPS, strict=True):
        a, b = switches(load_config(before)), switches(load_config(after))
        changed = {k for k in SWITCHES if a[k] != b[k]}
        assert changed and changed <= group, f"{before} -> {after} changed {changed}"


def test_baselines_match_the_proposal(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")
    b0, b1, b2, b3 = (load_config(n) for n in ["b0", "b1", "b2", "b3"])
    assert b0["closed_book"] is True
    assert (b1["retrieval_mode"], b1["chunk_strategy"], b1["rerank"]) == ("bm25", "fixed@v1", False)
    assert (b2["retrieval_mode"], b2["chunk_strategy"], b2["rerank"]) == (
        "dense",
        "fixed@v1",
        False,
    )
    assert (b3["retrieval_mode"], b3["chunk_strategy"], b3["rerank"], b3["gate"]) == (
        "hybrid",
        "struct@v1",
        True,
        True,
    )
    assert len(b3["checks"]) == 4


def test_fixed_settings_match_the_stack(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")
    cfg = load_config("b3")
    assert cfg["vector_db"]["distance"] == "cosine"
    assert cfg["embedding"] == {
        "model": "BAAI/bge-small-en-v1.5",
        "dimension": 384,
        "normalize": True,
        "batch_size": 64,
    }
    assert (cfg["retrieval"]["pool_size"], cfg["retrieval"]["rrf_k"]) == (20, 60)
    assert cfg["rerank_settings"]["top_n"] == 5
    assert cfg["generation"]["temperature"] == 0
    assert cfg["checks_settings"]["quote_max_words"] == 25


def test_unknown_config_name_fails_clearly():
    with pytest.raises(FileNotFoundError, match="no config named"):
        load_config("b9")


def test_validation_catches_bad_switches():
    good = {k: None for k in SWITCHES} | {
        "closed_book": False,
        "retrieval_mode": "dense",
        "rerank": False,
        "gate": False,
        "checks": [],
    }
    assert validate_config(good) == []
    assert validate_config(good | {"retrieval_mode": "semantic"})
    assert validate_config(good | {"closed_book": True, "rerank": True})
    assert validate_config(good | {"gate_threshold": "high"})
    assert validate_config({"closed_book": False})
