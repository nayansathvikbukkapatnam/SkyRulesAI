"""Hand-labelled failures for report 06 (DELIVERABLES section 4: 15+ labelled failures).

Two steps, with a person in between:

    python -m src.eval.failure_labels sheet --config b3,b2 --split dev
        Writes reports/failure_labels_<split>.jsonl: one line per failed item of each listed
        eval run (an incorrect answer, an over-refusal, or an answer to an unanswerable item),
        with what a person needs to judge it and empty "label", "note" and "labelled_by".

    (a person opens the file and fills in "label", "note" and "labelled_by" for each line)

    python -m src.eval.failure_labels count --split dev
        Checks every label against the allowed list and that each labelled line names who
        labelled it, then writes reports/artifacts/failure_labels_<split>.json with counts
        per category.

Claude Code never fills in labels (they are human judgments, like the golden set).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.common import paths

LABELS = (
    "retrieval_miss",
    "rank_failure",
    "chunk_boundary",
    "extraction_failure",
    "stale_content",
    "reranker_demotion",
    "generator_ignored_context",
    "invalid_citation",
    "over_refusal",
    "hallucination",
)
MINIMUM = 15
TOP_CHUNKS = 5


def sheet_path(split: str) -> Path:
    return paths.REPORTS / f"failure_labels_{split}.jsonl"


def failure_type(item: dict) -> str | None:
    """Why an eval item counts as a failure, or None when it passed."""
    m = item.get("metrics") or {}
    if item.get("error"):
        return "error"
    if m.get("over_refused") is True:
        return "over_refusal"
    if m.get("refused_correctly") is False:
        return "answered_unanswerable"
    if m.get("correct") is False:
        return "incorrect_answer"
    return None


def sheet_line(item: dict, gold: dict, config: str, split: str) -> dict[str, Any]:
    answer = item.get("answer") or {}
    trace = answer.get("trace") or {}
    judge = (item.get("metrics") or {}).get("judge") or {}
    return {
        "id": item["id"],
        "config": config,
        "split": split,
        "category": item.get("category"),
        "question": item.get("question"),
        "gold_answer": gold.get("gold_answer"),
        "gold_source_url": gold.get("source_url"),
        "failure_type": failure_type(item),
        "status": answer.get("status"),
        "sentences": answer.get("sentences") or [],
        "citations": [
            {"n": c.get("n"), "chunk_id": c.get("chunk_id"), "url": c.get("url")}
            for c in answer.get("citations") or []
        ],
        "refusal_reason": trace.get("refusal_reason"),
        "judge_reason": judge.get("reason"),
        "error": item.get("error"),
        "top_chunk_ids": (item.get("retrieved_chunk_ids") or [])[:TOP_CHUNKS],
        "label": "",
        "note": "",
        "labelled_by": "",
    }


def read_jsonl(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def make_sheet(
    eval_paths: dict[str, Path], golden_path: Path, out: Path, split: str, force: bool = False
) -> list[dict]:
    """One line per failed item of each run in `eval_paths` ({config: eval file}), in order."""
    if out.exists() and not force and any(line.get("label") for line in read_jsonl(out)):
        raise SystemExit(f"{out} already has human labels; refusing to overwrite (use --force)")
    gold = {g["id"]: g for g in read_jsonl(golden_path)} if golden_path.exists() else {}
    lines = []
    for config, eval_path in eval_paths.items():
        run = json.loads(eval_path.read_text(encoding="utf-8"))
        lines += [
            sheet_line(item, gold.get(item["id"], {}), config, split)
            for item in run["items"]
            if failure_type(item)
        ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return lines


def count_labels(lines: list[dict]) -> dict[str, Any]:
    """Validate the human labels and count them. Raises ValueError on a bad line."""
    problems = []
    for line in lines:
        label = (line.get("label") or "").strip()
        if not label:
            continue
        if label not in LABELS:
            problems.append(f"{line['id']}: label {label!r} is not one of {', '.join(LABELS)}")
        if not (line.get("labelled_by") or "").strip():
            problems.append(f"{line['id']}: labelled but labelled_by is empty")
    if problems:
        raise ValueError("\n".join(problems))
    labelled = [line for line in lines if (line.get("label") or "").strip()]
    counts = Counter(line["label"].strip() for line in labelled)
    return {
        "counts": {label: counts.get(label, 0) for label in LABELS},
        "total": len(labelled),
        "unlabelled": len(lines) - len(labelled),
        "minimum": MINIMUM,
        "meets_minimum": len(labelled) >= MINIMUM,
        "labelled_by": sorted({line["labelled_by"].strip() for line in labelled}),
        "configs": sorted({line.get("config") or "" for line in labelled}),
        "by_config": dict(Counter(line.get("config") for line in labelled)),
        "by_failure_type": dict(Counter(line.get("failure_type") for line in labelled)),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Failure-label sheet and counts for report 06.")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sheet", help="write the sheet of failed items to label")
    s.add_argument("--config", default="b3", help="one config, or several: b3,b2")
    s.add_argument("--split", default="dev")
    s.add_argument("--force", action="store_true", help="overwrite a sheet that has labels")
    c = sub.add_parser("count", help="validate labels and write the counts artifact")
    c.add_argument("--split", default="dev")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "sheet":
        configs = [c.strip() for c in args.config.split(",") if c.strip()]
        eval_paths = {c: paths.ARTIFACTS / f"eval_{c}_{args.split}.json" for c in configs}
        missing = [c for c, p in eval_paths.items() if not p.exists()]
        if missing:
            print(f"no eval file for {', '.join(missing)}; run make eval SPLIT={args.split}")
            return 1
        lines = make_sheet(eval_paths, paths.GOLDEN, sheet_path(args.split), args.split, args.force)
        print(f"wrote {len(lines)} failed items to {sheet_path(args.split)}; label them by hand")
        return 0

    path = sheet_path(args.split)
    if not path.exists():
        print(f"{path} not found; run the sheet command first")
        return 1
    try:
        result = count_labels(read_jsonl(path))
    except ValueError as e:
        print(f"invalid labels:\n{e}")
        return 1
    result.update(
        {
            "command": f"python -m src.eval.failure_labels count --split {args.split}",
            "split": args.split,
            "computed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    paths.ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = paths.ARTIFACTS / f"failure_labels_{args.split}.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}: {result['total']} labelled ({result['unlabelled']} still empty)")
    if not result["meets_minimum"]:
        print(f"below the minimum of {MINIMUM} labelled failures required for report 06")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
