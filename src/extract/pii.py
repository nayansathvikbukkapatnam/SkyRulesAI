"""Redact personal contact details from extracted text (CLAUDE.md rule 2, ethics safeguard 1).

Emails, phone numbers and street addresses are replaced with a placeholder and
counted; the counts (never the matched values) go to reports/artifacts/pii_log.json.
Whole contact blocks are dropped earlier, at extraction (see html.FR_SKIP_HEADINGS).
"""

from __future__ import annotations

import re
from collections import Counter

PATTERNS = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    # (202) 267-1234, 202-267-1234, 202.267.1234, 1-844-359-6982; not docket or FR numbers.
    "phone": re.compile(
        r"(?<![\w-])(?:1[-.\s])?(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.\s]\d{4}(?![\w-])"
    ),
    "street_address": re.compile(
        r"\b\d{1,5}\s+(?:[A-Z][A-Za-z]+\.?\s+){1,4}"
        r"(?:Street|St\.|Avenue|Ave\.|Road|Rd\.|Boulevard|Blvd\.|Drive|Dr\.|Lane|Ln\.|Way|Place|Court)"
        r"(?:,?\s+(?:SW|SE|NW|NE|S\.W\.|S\.E\.|N\.W\.|N\.E\.))?\b"
    ),
}
PLACEHOLDER = "[REDACTED {kind}]"


def redact(text: str) -> tuple[str, Counter]:
    """Return the text with every match replaced, and the count per kind."""
    counts: Counter = Counter()
    for kind, pattern in PATTERNS.items():
        text, n = pattern.subn(PLACEHOLDER.format(kind=kind.upper()), text)
        if n:
            counts[kind] += n
    return text, counts
