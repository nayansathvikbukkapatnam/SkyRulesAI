"""Report 05 (generation): fill its tables from reports/artifacts/, and check the judge.

Same convention as reports 02 and 04: every number sits between
    <!-- BEGIN AUTO:<block> -->  ...  <!-- END AUTO:<block> -->
markers and is rewritten from the artifacts the make targets wrote (rule 6). A
missing artifact renders as "not produced yet" with the command that makes it.

    python -m src.reports.generation                              # fill the report
    python -m src.reports.generation label-sheet --split dev      # 20 answers to hand-label
    python -m src.reports.generation agreement --split dev        # judge vs your labels

The label sheet is blind: it shows the question, the gold answer and the system's
answer, never the judge's verdict, so the hand labels are independent. Claude Code
never fills in labels (rule 4 spirit: the check against humans must be human).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.common import paths
from src.common.config import load_config

REPORT = paths.REPORTS / "05_generation.md"
CONFIGS = ("b0", "b1", "b2", "a1", "a2", "a3", "b3")
MAIN = "b3"
STAGES = ("route", "retrieve", "rerank", "generate", "check", "total")
LABEL_SAMPLE = 20
SEED = 42

# Refusal reasons from answer() (trace.refusal_reason), grouped for the report.
REASON_GROUPS = (
    (re.compile(r"^evidence gate"), "Evidence gate"),
    (re.compile(r"^not enough support"), "Not enough support (model said insufficient)"),
    (re.compile(r"^check failed: (.+)"), "Failed check: {}"),
    (re.compile(r"^check error"), "Check could not run"),
    (re.compile(r"^generation error"), "Generation error (unusable reply)"),
    (re.compile(r"^nothing left to answer"), "Only injected instructions"),
)


def labels_path(split: str) -> Path:
    return paths.REPORTS / f"judge_labels_{split}.jsonl"


# --- Formatting -------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    """Plain cells; "n/a" for a missing value (no dashes in the report)."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") if value % 1 else f"{value:.0f}"
    return str(value)


def _usd(value: Any) -> str:
    """Dollar amounts to 6 decimals: per-query costs are fractions of a cent."""
    return "n/a" if value is None else f"{value:.6f}".rstrip("0").rstrip(".")


def _table(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_fmt(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _missing(what: str, command: str) -> str:
    return f"_Not produced yet: {what}. Run `{command}`._"


def _load(artifacts: Path, name: str) -> Any | None:
    path = artifacts / name
    return json.loads(path.read_text()) if path.exists() else None


def _runs(artifacts: Path, split: str) -> dict[str, dict]:
    """The eval runs present for this split, in ablation order."""
    runs = {}
    for config in CONFIGS:
        run = _load(artifacts, f"eval_{config}_{split}.json")
        if run is not None:
            runs[config] = run
    return runs


def _rate(entry: dict | None) -> str:
    if not entry or entry.get("rate") is None:
        return "n/a"
    return f"{_fmt(entry['rate'])} (n={entry['n']})"


# --- Blocks -------------------------------------------------------------------------------


def block_rates(artifacts: Path, split: str) -> str:
    runs = _runs(artifacts, split)
    if not runs:
        return _missing(f"eval runs on {split}", f"make eval CONFIG=b3 SPLIT={split}")
    rows = []
    for config, run in runs.items():
        s = run["summary"]
        rows.append([
            config.upper(), s["n_items"], s["n_answered"], s["n_errors"],
            _rate(s.get("correctness")), _rate(s.get("citation_validity")),
            _rate(s.get("correct_refusal")), _rate(s.get("over_refusal")),
        ])  # fmt: skip
    header = ["Config", "Items", "Answered", "Errors", "Correct", "Citation valid",
              "Correct refusal (unanswerable)", "Over-refusal (answerable)"]  # fmt: skip
    commits = sorted({f"{c.upper()} {r.get('git_commit', 'unknown')}" for c, r in runs.items()})
    note = (
        f"Source: `reports/artifacts/eval_<config>_{split}.json` (`make eval CONFIG=<config> "
        f"SPLIT={split}`), commits: {', '.join(commits)}. Citation validity is not applicable "
        "to B0 (closed book cites nothing). Intervals and paired tests are in report 06."
    )
    return _table(header, rows) + "\n\n" + note


def _reason_group(reason: str | None) -> str:
    for pattern, label in REASON_GROUPS:
        m = pattern.match(reason or "")
        if m:
            return label.format(*m.groups())
    return "Other" if reason else "Unknown"


def block_refusals(artifacts: Path, split: str) -> str:
    run = _load(artifacts, f"eval_{MAIN}_{split}.json")
    if run is None:
        return _missing(f"the {MAIN.upper()} eval run", f"make eval CONFIG={MAIN} SPLIT={split}")
    refused = [
        it for it in run["items"] if it.get("answer") and it["answer"].get("status") == "refused"
    ]
    errors = sum(1 for it in run["items"] if it.get("error"))
    if not refused:
        return f"No {MAIN.upper()} refusals on {split} ({errors} pipeline errors)."
    counts = Counter(_reason_group(it["answer"]["trace"].get("refusal_reason")) for it in refused)
    by_cat = Counter(
        (
            it.get("category") == "unanswerable",
            _reason_group(it["answer"]["trace"].get("refusal_reason")),
        )
        for it in refused
    )
    rows = [
        [reason, n, by_cat[(True, reason)], by_cat[(False, reason)]]
        for reason, n in counts.most_common()
    ]
    table = _table(
        ["Refusal reason", "Refusals", "On unanswerable items", "On answerable items"], rows
    )
    return table + (
        f"\n\nSource: `trace.refusal_reason` in `eval_{MAIN}_{split}.json`. "
        f"{len(refused)} refusals; {errors} pipeline errors are counted separately."
    )


def block_checks(artifacts: Path, split: str) -> str:
    run = _load(artifacts, f"eval_{MAIN}_{split}.json")
    if run is None:
        return _missing(f"the {MAIN.upper()} eval run", f"make eval CONFIG={MAIN} SPLIT={split}")
    ran = Counter()
    failed = Counter()
    sentences_failed = Counter()
    for it in run["items"]:
        checks = ((it.get("answer") or {}).get("trace") or {}).get("checks") or {}
        for name, ok in (checks.get("per_check") or {}).items():
            ran[name] += 1
            failed[name] += 0 if ok else 1
        for sent in checks.get("sentences") or []:
            for name in checks.get("requested") or []:
                verdict = sent.get(name) or {}
                key = "supported" if name == "claim_support" else "ok"
                if verdict and not verdict.get(key, True):
                    sentences_failed[name] += 1
    if not ran:
        return f"No answer reached the checks in {MAIN.upper()} on {split}."
    labels = {"citation_validity": "Citations", "claim_support": "Claims",
              "number_date": "Numbers", "quote_limit": "Quotes (25-word limit)"}  # fmt: skip
    rows = [[labels.get(n, n), ran[n], failed[n], sentences_failed[n]] for n in sorted(ran)]
    return _table(
        ["Check", "Answers checked", "Answers failed (refused)", "Sentences failed"], rows
    ) + (
        f"\n\nSource: `trace.checks` in `eval_{MAIN}_{split}.json`. Any failed check refuses "
        "the whole answer (fail closed), so no failed sentence was shown to a user."
    )


def block_referrals(artifacts: Path, split: str) -> str:
    run = _load(artifacts, f"eval_{MAIN}_{split}.json")
    if run is None:
        return _missing(f"the {MAIN.upper()} eval run", f"make eval CONFIG={MAIN} SPLIT={split}")
    tags = Counter()
    referred = Counter()
    for it in run["items"]:
        a = it.get("answer")
        if not a:
            continue
        tags[a.get("router_tag")] += 1
        if a.get("referral"):
            referred[(a.get("router_tag"), a["referral"].get("kind"), a.get("status"))] += 1
    rows = [[tag, n] for tag, n in tags.most_common()]
    ref_rows = [[t, k, s, n] for (t, k, s), n in sorted(referred.items())]
    out = _table(["Router tag", "Questions"], rows)
    if ref_rows:
        out += "\n\n" + _table(["Router tag", "Referral", "Answer status", "Questions"], ref_rows)
    return out + f"\n\nSource: `router_tag` and `referral` in `eval_{MAIN}_{split}.json`."


def block_latency_cost(artifacts: Path, split: str) -> str:
    runs = _runs(artifacts, split)
    if not runs:
        return _missing(f"eval runs on {split}", f"make eval CONFIG=b3 SPLIT={split}")
    rows = []
    for config, run in runs.items():
        s = run["summary"]
        lat = s.get("latency_ms") or {}
        rows.append(
            [config.upper()]
            + [
                f"{_fmt((lat.get(st) or {}).get('p50'))} / {_fmt((lat.get(st) or {}).get('p95'))}"
                for st in STAGES
            ]
            + [_usd(s.get("cost_usd_per_query")), _usd(s.get("cost_usd_total"))]
        )
    header = (
        ["Config"]
        + [f"{st} p50 / p95 (ms)" for st in STAGES]
        + ["Cost per query (USD)", "Total (USD)"]
    )
    return _table(header, rows) + (
        f"\n\nSource: `summary.latency_ms` and `summary.cost_usd_*` in "
        f"`eval_<config>_{split}.json`. Latency is measured on one laptop (CPU retrieval and "
        "rerank, Claude API calls); a cached LLM reply keeps its original latency."
    )


def block_settings(artifacts: Path, split: str) -> str:
    gen = load_config(MAIN)["generation"]
    prices = gen.get("prices", {}).get("anthropic", {})
    rows = [
        ["temperature", _fmt(gen.get("temperature")).replace("n/a", "null (not sent)"),
         "Haiku 5.5 rejects any value but 1"],
        ["thinking", gen.get("thinking"), "off, so the answer is the reply text only"],
        ["effort", gen.get("effort"), ""],
        ["max_tokens", gen.get("max_tokens"), "a reply cut off here is refused (truncated)"],
        ["price per million input / output tokens (USD)",
         f"{_fmt(prices.get('input_per_mtok_usd'))} / {_fmt(prices.get('output_per_mtok_usd'))}",
         f"Claude API, prompts under {_fmt(prices.get('max_input_tokens'))} tokens"],
    ]  # fmt: skip
    return _table(["Setting", "Value", "Note"], rows) + (
        "\n\nSource: `generation` in `configs/default.yaml` (the frozen B3 config)."
    )


def block_judge(artifacts: Path, split: str) -> str:
    a = _load(artifacts, f"judge_agreement_{split}.json")
    if a is None:
        return _missing(
            "the judge check against hand labels",
            f"python -m src.reports.generation label-sheet --split {split}, label it, then "
            f"python -m src.reports.generation agreement --split {split}",
        )
    m = a["matrix"]
    rows = [
        ["judge: correct", m["judge_yes_human_yes"], m["judge_yes_human_no"]],
        ["judge: incorrect", m["judge_no_human_yes"], m["judge_no_human_no"]],
    ]
    return _table(["", "human: correct", "human: incorrect"], rows) + (
        f"\n\n{a['n']} answers on {split} "
        f"({', '.join(f'{c.upper()} {k}' for c, k in a['per_config'].items())}), labelled blind by "
        f"{', '.join(a['labeled_by'])}. Agreement {_fmt(a['agreement'])}, Cohen's kappa "
        f"{_fmt(a['kappa'])}. Disagreements: {', '.join(a['disagreements']) or 'none'}. "
        f"Source: `reports/artifacts/judge_agreement_{split}.json`."
    )


BLOCKS = {
    "rates": block_rates,
    "refusals": block_refusals,
    "checks": block_checks,
    "referrals": block_referrals,
    "latency_cost": block_latency_cost,
    "settings": block_settings,
    "judge": block_judge,
}
_BLOCK = re.compile(r"(<!-- BEGIN AUTO:(\w+) -->)\n(.*?)\n(<!-- END AUTO:\2 -->)", re.S)


def render(text: str, artifacts: Path, split: str = "dev") -> str:
    return _BLOCK.sub(
        lambda m: f"{m.group(1)}\n{BLOCKS[m.group(2)](artifacts, split)}\n{m.group(4)}", text
    )


# --- Judge check against hand labels -------------------------------------------------------


def _golden(golden_path: Path) -> dict[str, dict]:
    return {
        it["id"]: it
        for it in (
            json.loads(line) for line in golden_path.read_text().splitlines() if line.strip()
        )
    }


def _graded(run: dict) -> list[dict]:
    """Answered, answerable items the judge scored, in id order."""
    return sorted(
        (
            it for it in run["items"]
            if it.get("answer") and it["answer"].get("status") == "answered"
            and (it.get("metrics") or {}).get("judge") is not None
            and it.get("category") != "unanswerable"
        ),
        key=lambda it: it["id"],
    )  # fmt: skip


def _as_runs(runs: dict) -> dict[str, dict]:
    """A config -> eval run mapping; a single eval run is also accepted."""
    return {runs["config"]: runs} if "items" in runs else runs


def label_sheet(
    runs: dict, golden: dict[str, dict], n: int = LABEL_SAMPLE, seed: int = SEED
) -> list[dict]:
    """A blind sample of n graded answers.

    runs maps config -> eval run in priority order: the main config is sampled first, and
    later configs top the sheet up to n when it has too few graded answers (the judge is
    the same for every config, so their answers check it just as well).
    """
    runs = _as_runs(runs)
    rng = random.Random(seed)
    picked: list[tuple[str, dict]] = []
    for config, run in runs.items():
        pool = _graded(run)
        picked += [(config, it) for it in rng.sample(pool, min(n - len(picked), len(pool)))]
        if len(picked) >= n:
            break
    order = list(runs)
    return [
        {
            "config": config,
            "id": it["id"],
            "category": it.get("category"),
            "question": it["question"],
            "gold_answer": golden.get(it["id"], {}).get("gold_answer"),
            "source_url": golden.get(it["id"], {}).get("source_url"),
            "system_answer": " ".join(s["text"] for s in it["answer"]["sentences"]),
            "human_correct": None,  # fill in: true or false
            "labeled_by": "",  # fill in: your name
            "note": "",
        }
        for config, it in sorted(picked, key=lambda p: (order.index(p[0]), p[1]["id"]))
    ]


def agreement(runs: dict, labels: list[dict]) -> dict[str, Any]:
    """Judge verdicts against hand labels, matched by (config, id). Raises if any label,
    name or judge verdict is missing."""
    from sklearn.metrics import cohen_kappa_score

    runs = _as_runs(runs)
    main_config = next(iter(runs))
    verdicts = {
        (config, it["id"]): ((it.get("metrics") or {}).get("judge") or {}).get("correct")
        for config, run in runs.items()
        for it in run["items"]
    }

    def key(lab: dict) -> tuple[str, str]:
        return lab.get("config", main_config), lab["id"]

    problems = [
        f"{key(lab)[0]}:{lab['id']}" for lab in labels
        if not isinstance(lab.get("human_correct"), bool)
        or not str(lab.get("labeled_by", "")).strip()
        or verdicts.get(key(lab)) is None
    ]  # fmt: skip
    if problems:
        raise ValueError(
            f"{len(problems)} label(s) are not ready (need human_correct true/false, labeled_by, "
            f"and a judge verdict): {problems[:5]}"
        )
    pairs = [(bool(verdicts[key(lab)]), lab["human_correct"]) for lab in labels]
    j = [p[0] for p in pairs]
    h = [p[1] for p in pairs]
    kappa = cohen_kappa_score(j, h) if len(set(j)) > 1 or len(set(h)) > 1 else 1.0
    used = sorted({key(lab)[0] for lab in labels})
    return {
        "configs": used,
        "per_config": dict(Counter(key(lab)[0] for lab in labels)),
        "split": next(iter(runs.values()))["split"],
        "eval_git_commits": {c: runs[c].get("git_commit") for c in used if c in runs},
        "n": len(pairs),
        "agreement": round(sum(a == b for a, b in pairs) / len(pairs), 4) if pairs else None,
        "kappa": None if kappa != kappa else round(float(kappa), 4),  # NaN when undefined
        "matrix": {
            "judge_yes_human_yes": sum(a and b for a, b in pairs),
            "judge_yes_human_no": sum(a and not b for a, b in pairs),
            "judge_no_human_yes": sum(not a and b for a, b in pairs),
            "judge_no_human_no": sum(not a and not b for a, b in pairs),
        },
        "disagreements": [
            f"{key(lab)[0]}:{lab['id']}"
            for lab, (a, b) in zip(labels, pairs, strict=True)
            if a != b
        ],
        "labeled_by": sorted({lab["labeled_by"].strip() for lab in labels}),
        "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


# --- CLI ----------------------------------------------------------------------------------------


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report 05: fill tables, and check the judge.")
    parser.add_argument(
        "command", nargs="?", default="fill", choices=["fill", "label-sheet", "agreement"]
    )
    parser.add_argument("--split", default="dev")
    parser.add_argument(
        "--configs", default="b3,b2", help="label-sheet and agreement: main config first"
    )
    parser.add_argument("--n", type=int, default=LABEL_SAMPLE)
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--artifacts", type=Path, default=paths.ARTIFACTS)
    parser.add_argument("--golden", type=Path, default=paths.GOLDEN)
    parser.add_argument("--labels", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    labels_file = args.labels or labels_path(args.split)

    if args.command == "fill":
        text = render(args.report.read_text(), args.artifacts, args.split)
        args.report.write_text(text)
        print(f"updated {args.report} ({text.count('_Not produced yet')} block(s) still waiting)")
        return 0

    if args.split == "test":  # the judge is checked on dev only (P6); test runs once (rule 5)
        raise SystemExit("hand labels are taken on the dev split only")
    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    runs = {
        c: run
        for c in configs
        if (run := _load(args.artifacts, f"eval_{c}_{args.split}.json")) is not None
    }
    if configs[0] not in runs:
        raise SystemExit(f"no eval_{configs[0]}_{args.split}.json; run make eval first")

    if args.command == "label-sheet":
        if labels_file.exists():
            raise SystemExit(f"{labels_file} exists; refusing to overwrite your labels")
        sheet = label_sheet(runs, _golden(args.golden), args.n)
        labels_file.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in sheet))
        per = ", ".join(f"{c} {k}" for c, k in Counter(r["config"] for r in sheet).items())
        print(f"wrote {len(sheet)} answers ({per}) to {labels_file}")
        print("set human_correct (true or false) and labeled_by on every line")
        if len(sheet) < args.n:
            print(
                f"WARNING: only {len(sheet)} graded answers in {', '.join(runs)}; "
                f"P6 asks for {args.n}"
            )
        return 0

    labels = [json.loads(line) for line in labels_file.read_text().splitlines() if line.strip()]
    result = agreement(runs, labels)
    args.artifacts.mkdir(parents=True, exist_ok=True)
    out = args.artifacts / f"judge_agreement_{args.split}.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"judge vs hand labels: n={result['n']}, "
        f"agreement {result['agreement']}, kappa {result['kappa']}"
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
