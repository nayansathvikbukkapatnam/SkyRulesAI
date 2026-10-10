"""Answer checks: citation validity and the 25-word quote limit (ethics safeguard 2).

Both run on the generator's JSON sentences, before anything is shown:

- citation_validity: every sentence cites at least one chunk_id, and every cited
  chunk_id is one of the retrieved (reranked top 5) chunks. A sentence that cites
  nothing, or cites a chunk it was never given, is dropped.
- quote_limit: no run of more than `max_words` consecutive words may match a
  retrieved chunk's text. Words are compared case-insensitively with punctuation
  and citation marks like [1] removed, so light reformatting does not hide a copy.
  Runs are also checked across sentence boundaries, so a long quote split into two
  short sentences is still caught.

Fail closed (CLAUDE.md rule 7): a malformed sentence fails; it is never skipped.
claim_check.py (Sai) covers claim support and the number and date match; answer.py
combines both modules' verdicts per sentence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.common.contracts import Sentence

CHECKS = ("citation_validity", "quote_limit")
MAX_QUOTE_WORDS = 25

_CITATION_MARK = re.compile(r"\[\d+\]")
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def words(text: str) -> list[str]:
    """Lowercase words without punctuation; citation marks such as [1] are removed."""
    return _WORD.findall(_CITATION_MARK.sub(" ", text).translate(_QUOTES).lower())


# --- Citation validity ------------------------------------------------------------


def citation_validity(
    sentences: Sequence[Sentence], retrieved_ids: Iterable[str]
) -> list[dict[str, Any]]:
    """Per sentence: {"ok", "reason"}. reason is None when ok."""
    retrieved = set(retrieved_ids)
    verdicts = []
    for sentence in sentences:
        ids = sentence.get("chunk_ids") if isinstance(sentence, Mapping) else None
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            verdicts.append({"ok": False, "reason": "malformed chunk_ids"})
        elif not ids:
            verdicts.append({"ok": False, "reason": "no citation"})
        else:
            unknown = sorted(set(ids) - retrieved)
            reason = f"cites chunks that were not retrieved: {unknown}" if unknown else None
            verdicts.append({"ok": not unknown, "reason": reason})
    return verdicts


# --- Quote limit ------------------------------------------------------------------


def longest_common_run(a: Sequence[str], b: Sequence[str]) -> tuple[int, int]:
    """Length of the longest run of consecutive words shared by a and b, and where it
    ends in a (exclusive). Dynamic programming, O(len(a) * len(b))."""
    best, best_end = 0, 0
    prev = [0] * (len(b) + 1)
    for i, word in enumerate(a, start=1):
        cur = [0] * (len(b) + 1)
        for j, other in enumerate(b, start=1):
            if word == other:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best, best_end = cur[j], i
        prev = cur
    return best, best_end


def quote_limit(
    sentences: Sequence[Sentence],
    chunk_texts: Mapping[str, str],
    max_words: int = MAX_QUOTE_WORDS,
) -> list[dict[str, Any]]:
    """Per sentence: {"ok", "longest_run", "chunk_id", "copied"}.

    Checked against every retrieved chunk, cited or not: copying is copying. The
    sentences are also joined in order, so a run crossing a sentence boundary fails
    every sentence it touches.
    """
    chunk_words = {cid: words(text) for cid, text in chunk_texts.items()}
    sentence_words = [
        words(s["text"]) if isinstance(s, Mapping) and isinstance(s.get("text"), str) else None
        for s in sentences
    ]
    verdicts: list[dict[str, Any]] = []
    for sw in sentence_words:
        if sw is None:
            verdicts.append({"ok": False, "longest_run": None, "chunk_id": None, "copied": None})
            continue
        verdicts.append(_worst_run(sw, chunk_words, max_words))

    # Across sentence boundaries: slide a (max_words + 1)-word window over the joined
    # answer; any window found in a chunk is an over-long copy, whoever it spans.
    joined: list[str] = []
    owner: list[int] = []
    for idx, sw in enumerate(sentence_words):
        for w in sw or []:
            joined.append(w)
            owner.append(idx)
    n = max_words + 1
    for cid, cw in chunk_words.items():
        grams = {tuple(cw[i : i + n]) for i in range(len(cw) - n + 1)}
        for start in range(len(joined) - n + 1):
            if tuple(joined[start : start + n]) not in grams:
                continue
            spanned = sorted(set(owner[start : start + n]))
            if len(spanned) < 2:
                continue  # inside one sentence: the per-sentence check reports it
            for idx in spanned:
                if verdicts[idx]["ok"]:
                    verdicts[idx] = {
                        "ok": False,
                        "longest_run": n,
                        "chunk_id": cid,
                        "copied": " ".join(joined[start : start + n]),
                        "across_sentences": spanned,
                    }
    return verdicts


def _worst_run(
    sentence_words: list[str], chunk_words: Mapping[str, list[str]], max_words: int
) -> dict[str, Any]:
    best_run, best_cid, best_end = 0, None, 0
    for cid, cw in chunk_words.items():
        run, end = longest_common_run(sentence_words, cw)
        if run > best_run:
            best_run, best_cid, best_end = run, cid, end
    copied = " ".join(sentence_words[best_end - best_run : best_end]) if best_run else ""
    return {
        "ok": best_run <= max_words,
        "longest_run": best_run,
        "chunk_id": best_cid if best_run > max_words else None,
        "copied": copied if best_run > max_words else None,
    }


# --- Whole answer -------------------------------------------------------------------


def check_sentences(
    sentences: Sequence[Sentence],
    chunk_texts: Mapping[str, str],
    checks: Iterable[str] = CHECKS,
    max_words: int = MAX_QUOTE_WORDS,
) -> dict[str, Any]:
    """Per-sentence verdicts for the requested checks, and `ok` for the whole answer.

    `chunk_texts` maps every retrieved chunk_id to its text. An answer with no
    sentences is not ok (there is nothing supported to show).
    """
    checks = tuple(checks)
    unknown = set(checks) - set(CHECKS)
    if unknown:
        raise ValueError(f"unknown check(s) {sorted(unknown)}; known: {list(CHECKS)}")

    results: dict[str, list[dict[str, Any]]] = {}
    if "citation_validity" in checks:
        results["citation_validity"] = citation_validity(sentences, chunk_texts.keys())
    if "quote_limit" in checks:
        results["quote_limit"] = quote_limit(sentences, chunk_texts, max_words)

    verdicts = []
    for i, sentence in enumerate(sentences):
        text = sentence.get("text") if isinstance(sentence, Mapping) else None
        v: dict[str, Any] = {"text": text, "chunk_ids": _ids(sentence)}
        for name, per_sentence in results.items():
            v[name] = per_sentence[i]
        v["ok"] = all(v[name]["ok"] for name in results)
        verdicts.append(v)
    return {"ok": bool(verdicts) and all(v["ok"] for v in verdicts), "sentences": verdicts}


def _ids(sentence: Any) -> list:
    ids = sentence.get("chunk_ids") if isinstance(sentence, Mapping) else None
    return list(ids) if isinstance(ids, list) else []
