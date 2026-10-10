"""Load a run configuration: configs/default.yaml, overlaid with configs/<name>.yaml.

Secrets and machine-specific values (AWS profile, region, model ID, crawl
contact email) come from the environment or a local .env, never from YAML.
"""

from __future__ import annotations

import copy
import os
from typing import Any

import yaml
from dotenv import load_dotenv

from src.common import paths
from src.common.contracts import RETRIEVAL_MODES

# The switches the ablation turns on one at a time (CLAUDE.md section 5).
SWITCHES = (
    "closed_book",
    "retrieval_mode",
    "chunk_strategy",
    "rerank",
    "gate",
    "gate_threshold",
    "checks",
    "generator_model",
)

ENV_KEYS = ("AWS_REGION", "AWS_PROFILE", "BEDROCK_MODEL_ID", "CRAWL_CONTACT_EMAIL")

_dotenv_loaded = False


def _load_env() -> None:
    global _dotenv_loaded
    if not _dotenv_loaded:
        # Real environment variables win over .env.
        load_dotenv(paths.ROOT / ".env", override=False)
        _dotenv_loaded = True


def env(key: str, required: bool = False) -> str | None:
    """Read one setting from the environment or .env. Never log the value."""
    _load_env()
    value = os.environ.get(key) or None
    if required and value is None:
        raise RuntimeError(f"{key} is not set; copy .env.example to .env and fill it in")
    return value


def _read_yaml(name: str) -> dict[str, Any]:
    path = paths.CONFIGS / f"{name}.yaml"
    if not path.exists():
        known = sorted(p.stem for p in paths.CONFIGS.glob("*.yaml"))
        raise FileNotFoundError(f"no config named {name!r}; known: {known}")
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(name: str) -> dict[str, Any]:
    """Return the merged config for a run, e.g. load_config("b2")."""
    cfg = _merge(_read_yaml("default"), _read_yaml(name)) if name != "default" else _read_yaml(name)
    cfg["name"] = name
    if cfg.get("generator_model") is None:
        cfg["generator_model"] = env("BEDROCK_MODEL_ID")
    errors = validate_config(cfg)
    if errors:
        raise ValueError(f"config {name!r} is invalid: " + "; ".join(errors))
    return cfg


def validate_config(cfg: dict[str, Any]) -> list[str]:
    errors = [f"missing switch '{k}'" for k in SWITCHES if k not in cfg]
    if errors:
        return errors
    if not cfg["closed_book"] and cfg["retrieval_mode"] not in RETRIEVAL_MODES:
        errors.append(f"retrieval_mode must be one of {sorted(RETRIEVAL_MODES)}")
    if cfg["closed_book"] and (cfg["rerank"] or cfg["gate"]):
        errors.append("closed_book runs cannot rerank or gate")
    threshold = cfg["gate_threshold"]
    if threshold is not None and not isinstance(threshold, (int, float)):
        errors.append("gate_threshold must be a number or null (null = not tuned yet)")
    return errors


def switches(cfg: dict[str, Any]) -> dict[str, Any]:
    """Only the ablation switches, for comparing two configs."""
    return {k: cfg[k] for k in SWITCHES}
