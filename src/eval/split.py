"""Split the golden set 60/40 into dev and test, stratified by category (make split).

- Runs the golden set checker first and refuses to split a set that fails it,
  so the split is only ever made from a complete, human-verified set.
- Uses sklearn train_test_split(stratify=category, random_state=42).
- Writes eval/split.json exactly once and refuses to overwrite it, so the test
  items can never be reshuffled after anyone has seen results.
- Records the golden file's SHA-256, so a later edit to the set is detected.

Every evaluation loads ids through load_split(), which refuses the test split
unless final=True (CLAUDE.md rule 5: the test split runs once, after b3-frozen).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sklearn.model_selection import train_test_split

from src.common import paths
from src.common.contracts import sha256_hex
from src.eval import golden_check

SEED = 42
TEST_SIZE = 0.4


class SplitError(RuntimeError):
    pass


def make_split(items: list[dict], seed: int = SEED, test_size: float = TEST_SIZE) -> dict:
    """Return {"dev": [ids], "test": [ids], "counts": {category: {dev, test}}}."""
    ids = [item["id"] for item in items]
    categories = [item["category"] for item in items]
    too_small = sorted(c for c, n in Counter(categories).items() if n < 2)
    if too_small:
        raise SplitError(f"each category needs 2+ items to stratify; too few: {too_small}")

    dev, test = train_test_split(
        ids, test_size=test_size, stratify=categories, random_state=seed
    )
    category_of = dict(zip(ids, categories, strict=True))
    counts = {
        cat: {
            "dev": sum(category_of[i] == cat for i in dev),
            "test": sum(category_of[i] == cat for i in test),
        }
        for cat in sorted(set(categories))
    }
    return {"dev": sorted(dev), "test": sorted(test), "counts": counts}


def run(golden: Path, normalized_dir: Path, out: Path) -> dict:
    if out.exists():
        raise SplitError(
            f"{out} already exists and is never overwritten. The test split must stay "
            "fixed once made; delete it only if the team agrees the split was never used."
        )
    check = golden_check.run(golden, normalized_dir)
    if not check["ok"]:
        raise SplitError(
            f"the golden set fails make golden-check ({len(check['errors'])} errors); "
            "fix it before splitting"
        )

    items, _ = golden_check.load_items(golden)
    result = make_split(items)
    record = {
        "seed": SEED,
        "test_size": TEST_SIZE,
        "method": "sklearn train_test_split, stratify=category",
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "golden_sha256": sha256_hex(golden.read_bytes()),
        "n_items": len(items),
        "n_dev": len(result["dev"]),
        "n_test": len(result["test"]),
        **result,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def load_split(
    name: str, final: bool = False, split_file: Path = paths.SPLIT, golden: Path = paths.GOLDEN
) -> list[str]:
    """Return the item ids of one split. The test split needs final=True (rule 5)."""
    if name not in ("dev", "test"):
        raise ValueError(f"split must be 'dev' or 'test', not {name!r}")
    if name == "test" and not final:
        raise SplitError(
            "the test split runs once, on Sunday, after the b3-frozen tag; "
            "pass --final only for that run"
        )
    if not split_file.exists():
        raise SplitError(f"{split_file} does not exist yet; run make split")
    record = json.loads(split_file.read_text(encoding="utf-8"))
    if golden.exists() and sha256_hex(golden.read_bytes()) != record["golden_sha256"]:
        raise SplitError(
            f"{golden} changed after the split was made; the split no longer matches the items"
        )
    return record[name]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--golden", type=Path, default=paths.GOLDEN)
    parser.add_argument("--normalized", type=Path, default=paths.NORMALIZED)
    parser.add_argument("--out", type=Path, default=paths.SPLIT)
    args = parser.parse_args(argv)

    try:
        record = run(args.golden, args.normalized, args.out)
    except SplitError as e:
        print(f"ERROR {e}")
        return 1
    print(f"split {record['n_items']} items: {record['n_dev']} dev, {record['n_test']} test")
    for cat, c in record["counts"].items():
        print(f"  {cat:<14} dev {c['dev']:>3}  test {c['test']:>3}")
    print(f"wrote {args.out} (seed {SEED})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
