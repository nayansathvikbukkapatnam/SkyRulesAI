"""Run a config on a golden split: answer() on every item, then the answer metrics.

    make eval CONFIG=b2 SPLIT=dev
    -> reports/artifacts/eval_<config>_<split>.json

One record per golden item (the section 5 eval results contract): id, category,
question, the full Answer, the retrieved chunk_ids, pass/fail per metric, and any
error. The run records the git commit, config switches and timestamp, plus the
run-level summary from answer_metrics.

The test split runs once (CLAUDE.md rule 5): SPLIT=test needs --final and is
refused entirely until the b3-frozen tag exists. All tuning uses SPLIT=dev.

An answer() that raises is recorded as an error and fails that item; the run
keeps going so one bad item never hides the rest.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.common import paths
from src.common.config import load_config, switches
from src.eval.answer_metrics import Judge, item_result, retrieved, summarize
from src.eval.golden_check import load_items
from src.eval.retrieval_metrics import split_ids

# answer(question, history, config) -> Answer (Nayan's src/generate/answer.py)
AnswerFn = Callable[[str, list, dict], dict]


def git_commit() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=paths.ROOT
    )
    return out.stdout.strip() or "unknown"


def evaluate(
    items: Iterable[dict], cfg: dict, answer_fn: AnswerFn, judge: Judge | None
) -> list[dict[str, Any]]:
    records = []
    for item in items:
        record: dict[str, Any] = {
            "id": item["id"],
            "category": item.get("category"),
            "question": item["question"],
            "answer": None,
            "retrieved_chunk_ids": [],
            "error": None,
        }
        try:
            answer = answer_fn(item["question"], [], cfg)
        except Exception as e:  # recorded, counted as a failure, run continues
            answer = None
            record["error"] = f"{type(e).__name__}: {e}"
        if answer is not None:
            record["answer"] = answer
            record["retrieved_chunk_ids"] = [c["chunk_id"] for c in retrieved(answer)]
        record["metrics"] = item_result(item, answer, judge)
        records.append(record)
    return records


def default_answer_fn() -> AnswerFn:
    try:
        from src.generate.answer import answer
    except ImportError as e:
        raise SystemExit(f"src.generate.answer is not available yet (N7): {e}") from e
    return answer


def default_judge() -> Judge:
    from src.generate.llm import LLMClient, make_judge

    return make_judge(LLMClient(load_config("default")))


def main(
    argv: Iterable[str] | None = None,
    answer_fn: AnswerFn | None = None,
    judge: Judge | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run a config on a golden split.")
    parser.add_argument("--config", required=True, help="b0 | b1 | b2 | a1 | a2 | a3 | b3")
    parser.add_argument("--split", default="dev")
    parser.add_argument("--final", action="store_true", help="the one test-split run (rule 5)")
    parser.add_argument(
        "--no-judge", action="store_true", help="skip the correctness judge (no LLM key needed)"
    )
    parser.add_argument("--golden", type=Path, default=paths.GOLDEN)
    parser.add_argument("--split-file", type=Path, default=paths.SPLIT)
    parser.add_argument("--artifacts", type=Path, default=paths.ARTIFACTS)
    args = parser.parse_args(list(argv) if argv is not None else None)

    cfg = load_config(args.config)
    ids = split_ids(args.split, args.split_file, args.final)
    items, errors = load_items(args.golden)
    if errors:
        raise ValueError(f"{args.golden}: {errors[0]} (run make golden-check)")
    items = [i for i in items if i.get("id") in ids]
    if not items:
        raise ValueError(f"no golden items match the {args.split} split in {args.split_file}")

    answer_fn = answer_fn or default_answer_fn()
    if judge is None and not args.no_judge:
        judge = default_judge()
    started = datetime.now(UTC)
    records = evaluate(items, cfg, answer_fn, judge)
    result = {
        "config": args.config,
        "split": args.split,
        "switches": switches(cfg),
        "git_commit": git_commit(),
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "judge": None if judge is None else "make_judge (configs/default.yaml generation)",
        "summary": summarize(records),
        "items": records,
    }
    args.artifacts.mkdir(parents=True, exist_ok=True)
    out = args.artifacts / f"eval_{args.config}_{args.split}.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")

    s = result["summary"]
    print(
        f"{args.config} on {args.split}: {s['n_items']} items ({s['n_errors']} errors); "
        f"correct {s['correctness']['rate']}, citation valid {s['citation_validity']['rate']}, "
        f"correct refusal {s['correct_refusal']['rate']}, over-refusal {s['over_refusal']['rate']}"
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
