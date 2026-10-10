"""Safety router: tag each question before retrieval (proposal section 7.1).

Pattern rules only, with no LLM call, so routing is fast, free and deterministic.
answer() calls route() first and uses the result to:

- add the referral (location or high_stakes) to the answer card,
- send the cleaned question (injection text removed) to retrieval,
- pass `instructions` to the prompt when the pilot type is unclear.

One tag is returned, by precedence: injection > location_live > high_stakes >
pilot_unclear > normal. `reasons` lists every rule that matched, for the trace
and the query log. The referral wording below is owned by Dorai (PM).
"""

from __future__ import annotations

import re
from typing import TypedDict

from src.common.contracts import Referral, RouterTag

B4UFLY_URL = "https://www.faa.gov/uas/getting_started/b4ufly"
FACILITY_MAPS_URL = "https://www.faa.gov/uas/commercial_operators/uas_facility_maps"

# --- Referral wording (shown on the answer card) -----------------------------

LOCATION_TEXT = (
    "SkyRules can't check whether a specific place is open to drones right now. "
    "Airspace, temporary flight restrictions and local rules depend on the exact place "
    "and time, and those live FAA maps aren't in the pages SkyRules searches. "
    "Check B4UFLY or the UAS Facility Maps for your exact location before you fly."
)
HIGH_STAKES_TEXT = (
    "A mistake here could put people or aircraft at risk or lead to FAA penalties. "
    "Treat this as general information from FAA pages and confirm with the FAA "
    "(faa.gov/uas) before you fly."
)
WAIVER_TEXT = (
    "Waivers and airspace authorizations are decided by the FAA case by case. "
    "SkyRules can explain the general rules, but only the FAA can approve a request."
)
EMERGENCY_TEXT = (
    "If anyone is hurt or in danger, call 911 first. SkyRules only explains general "
    "FAA rules; contact the FAA directly about anything that has already happened."
)
# Sent to the prompt (not shown as a referral) when the pilot type is unclear.
PILOT_UNCLEAR_INSTRUCTIONS = (
    "The question does not say whether the person flies for fun or under Part 107. "
    "Answer both cases in two labelled parts, 'If you fly for fun' and "
    "'If you fly under Part 107', using only the numbered passages."
)

PILOT_TYPES = ("recreational", "part107", "not_sure")


class RouteResult(TypedDict):
    router_tag: RouterTag
    referral: Referral | None
    reasons: list[str]  # every rule that matched, e.g. ["location:near me", "high_stakes:airport"]
    question: str  # the question to retrieve and answer, with injection text removed
    instructions: str | None  # extra prompt instructions (pilot_unclear only)


def _rx(*patterns: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


# --- Injection: instructions aimed at the bot rather than a drone question ---

_INJECTION = _rx(
    # Needs "your/previous/system..." so "Can I ignore the FAA rules?" is not flagged.
    r"\b(?:ignore|disregard|forget|override|bypass)\s+(?:all\s+|any\s+)?"
    r"(?:your|the\s+(?:previous|prior|above|earlier|system)|previous|prior|above|earlier"
    r"|preceding|system|these|those)\b[\w\s-]{0,30}?"
    r"\b(?:instructions?|rules|prompts?|directions|guidelines|guardrails|context|constraints)\b",
    # Needs "your/system" so "Show me the instructions for registering" is not flagged.
    r"\b(?:reveal|show|print|repeat|output|leak)\b[\w\s]{0,10}?\b(?:your|system)\s+"
    r"(?:system\s+)?(?:prompt|instructions)\b",
    r"\byou\s+are\s+now\b",
    r"\b(?:act|behave)\s+as\s+(?:an?\s+)?(?:unrestricted|different|new)\b",
    r"\bpretend\s+(?:to\s+be|you\s+are)\b",
    r"\b(?:developer|dan|god)\s+mode\b",
    r"\bjailbreak",
    r"^\s*(?:system|new\s+instructions)\s*:",
    r"</?\s*system\s*>|\[\s*system\s*\]",
    r"\b(?:answer|respond)\b[\w\s]{0,20}\bwithout\b[\w\s]{0,10}\b(?:citations?|citing|sources)\b",
    r"\buse\s+your\s+own\s+knowledge\b",
    r"\b(?:do\s+not|don'?t|never)\s+refuse\b",
)

# --- Location-specific or live questions --------------------------------------

_LIVE_TIME = _rx(
    r"\btonight\b",
    r"\bright\s+now\b",
    r"\btoday\b",
    r"\btomorrow\b",
    r"\bthis\s+(?:morning|afternoon|evening|weekend)\b",
    r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)",
)
_LIVE_PLACE = _rx(
    r"\bnear\s+me\b",
    r"\bnearby\b",
    # "here" is a place only after a flying verb ("Can I fly my drone here?") or at the
    # end of the question; "Is there a rule here about night flying?" is not.
    r"\b(?:fly|flying|launch|launching|land|landing|take\s+off|operate|operating)\b"
    r"(?:\s+(?:a|an|my|the|our|this|that|your))?(?:\s+(?:drone|uas|uav|aircraft|quadcopter))?"
    r"\s+(?:right\s+)?here\b",
    r"\bhere\s*\?*\s*$",
    r"\bmy\s+(?:house|home|backyard|yard|neighbou?rhood|street|city|town|area|location|school)\b",
    r"\bthis\s+(?:park|area|location|place|spot|beach|field)\b",
    r"\bzip\s*code\b",
    # A ZIP code, but not a 5-digit quantity ("10000 feet", "25000 lb") or part of an ID.
    r"(?<![\w.,$-])\d{5}(?![\w.,-])(?!\s*(?:feet|foot|ft|lbs?|pounds?|grams?|kg|kilograms?"
    r"|meters?|miles?|mi|knots?|kts?|mph|percent|%|dollars?)\b)",
)
_TFR = _rx(r"\btfrs?\b", r"\btemporary\s+flight\s+restrictions?\b")

# Capitalized words after a preposition are usually a place ("in San Diego",
# "over Balboa Park"), except the FAA terms and other non-places below.
_PLACE_AFTER_PREP = re.compile(
    r"\b(?i:in|at|over|near|around|above|by)\s+(?i:the\s+)?"
    r"([A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){0,3})"
)
_NOT_PLACES = {
    w.casefold()
    for w in """
    FAA UAS UAV UAG sUAS Part Class Remote ID TRUST LAANC DroneZone B4UFLY IACRA FRIA
    Recreational Exception Limited Operations Section Knowledge Test AC Advisory Circular
    Unmanned Aircraft General Small Standard Broadcast Module Airman Certification CFR Title
    English Spanish Night Waiver Certificate I The A An My Your Order Rule Rules Act
    Know Before Federal Register GovInfo Code Regulations Subpart Subchapter Chapter
    Appendix Docket Amendment Paragraph Volume Notice NPRM
    U.S. US USA America United States
    January February March April May June July August September October November December
    Monday Tuesday Wednesday Thursday Friday Saturday Sunday
    """.split()
}
# Lowercase place names the capitalization rule would miss.
_KNOWN_PLACES = _rx(
    r"\b(?:alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida"
    r"|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland"
    r"|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada"
    r"|new\s+hampshire|new\s+jersey|new\s+mexico|new\s+york|north\s+carolina|north\s+dakota"
    r"|ohio|oklahoma|oregon|pennsylvania|rhode\s+island|south\s+carolina|south\s+dakota"
    r"|tennessee|texas|utah|vermont|virginia|washington|west\s+virginia|wisconsin|wyoming)\b",
    r"\b(?:san\s+diego|los\s+angeles|san\s+francisco|chicago|houston|phoenix|seattle|miami"
    r"|boston|denver|atlanta|las\s+vegas|dallas|austin|nyc|d\.?c\.?|manhattan|brooklyn)\b",
)

# --- High-stakes topics ------------------------------------------------------

_HIGH_STAKES = {
    "airport": _rx(r"\bairports?\b", r"\bairfields?\b", r"\bheliports?\b", r"\brunways?\b"),
    "stadium": _rx(r"\bstadiums?\b", r"\barenas?\b", r"\bsporting\s+events?\b", r"\bgame\s+day\b"),
    "infrastructure": _rx(
        r"\bcritical\s+infrastructure\b",
        r"\bpower\s+(?:plants?|stations?|lines?)\b",
        r"\bnuclear\b",
        r"\bprisons?\b",
        r"\bjails?\b",
        r"\bmilitary\s+(?:bases?|installations?)\b",
        r"\bdams?\b",
        r"\brefiner(?:y|ies)\b",
    ),
    "waiver": _rx(r"\bwaivers?\b", r"\bexemptions?\b"),
    "emergency": _rx(
        r"\bemergenc(?:y|ies)\b",
        r"\bwildfires?\b",
        r"\bcrash(?:ed|es)?\b",
        r"\baccidents?\b",
        r"\binjur(?:y|ies|ed)\b",
        r"\bhurt\b",
        r"\bfly-?aways?\b",
    ),
    "people": _rx(r"\bover\s+(?:people|crowds?)\b", r"\bcrowds?\b"),
    "bvlos": _rx(r"\bbeyond\s+(?:the\s+)?visual\s+line\s+of\s+sight\b", r"\bbvlos\b"),
}

# --- Pilot type --------------------------------------------------------------

_CREDENTIAL = _rx(
    r"\blicen[cs]es?\b", r"\bcertificates?\b", r"\bcertification\b", r"\bcertified\b",
    r"\bpermits?\b",
)
_PILOT_STATED = _rx(
    r"\brecreational(?:ly)?\b", r"\bhobby(?:ist)?\b", r"\bfor\s+fun\b", r"\bfor\s+pleasure\b",
    r"\bpersonal\s+use\b", r"\btrust\b",
    r"\bpart\s*107\b", r"\bremote\s+pilot\b", r"\bcommercial(?:ly)?\b", r"\bbusiness\b",
    r"\bpaid\b", r"\bget\s+paid\b", r"\bfor\s+money\b", r"\bsell\b", r"\bclients?\b",
    r"\bjob\b", r"\bfor\s+work\b", r"\breal\s+estate\b", r"\bweddings?\b", r"\bcompany\b",
    r"\bpublic\s+safety\b", r"\bpolice\b", r"\blaw\s+enforcement\b", r"\bfire\s+department\b",
    r"\bgovernment\s+agency\b",
)


def _strip_injection(question: str) -> tuple[str, bool]:
    """Drop the sentences that carry injected instructions; keep the rest."""
    sentences = re.split(r"(?<=[.!?;])\s+|\n+", question.strip())
    kept = [s for s in sentences if s and not _INJECTION.search(s)]
    return " ".join(kept).strip(), len(kept) < len([s for s in sentences if s])


def _places(question: str) -> list[str]:
    found = []
    for m in _PLACE_AFTER_PREP.finditer(question):
        words = m.group(1).split()
        if words[0].casefold().rstrip(".") not in _NOT_PLACES:
            found.append(m.group(1))
    found += [m.group(0) for m in _KNOWN_PLACES.finditer(question)]
    return found


def route(question: str, pilot_type: str | None = "not_sure") -> RouteResult:
    """Tag a question and build its referral. pilot_type comes from the UI toggle."""
    if pilot_type not in (*PILOT_TYPES, None):
        raise ValueError(f"pilot_type must be one of {PILOT_TYPES}, not {pilot_type!r}")

    cleaned, injected = _strip_injection(question)
    reasons: list[str] = ["injection"] if injected else []
    q = cleaned

    location = [f"location:{p}" for p in _places(q)]
    location += [f"location:{m.group(0).lower()}" for m in _LIVE_PLACE.finditer(q)]
    location += [f"live:{m.group(0).lower()}" for m in _LIVE_TIME.finditer(q)]
    location += [f"live:{m.group(0).lower()}" for m in _TFR.finditer(q)]
    reasons += location

    stakes = [name for name, rx in _HIGH_STAKES.items() if rx.search(q)]
    reasons += [f"high_stakes:{s}" for s in stakes]

    unclear = (
        pilot_type in ("not_sure", None)
        and _CREDENTIAL.search(q) is not None
        and _PILOT_STATED.search(q) is None
    )
    if unclear:
        reasons.append("pilot_unclear")

    referral: Referral | None = None
    if location:
        referral = {"kind": "location", "text": LOCATION_TEXT}
    elif stakes:
        text = HIGH_STAKES_TEXT
        if "waiver" in stakes:
            text = f"{WAIVER_TEXT} {text}"
        if "emergency" in stakes:
            text = f"{EMERGENCY_TEXT} {text}"
        referral = {"kind": "high_stakes", "text": text}

    if injected:
        tag: RouterTag = "injection"
    elif location:
        tag = "location_live"
    elif stakes:
        tag = "high_stakes"
    elif unclear:
        tag = "pilot_unclear"
    else:
        tag = "normal"

    return {
        "router_tag": tag,
        "referral": referral,
        "reasons": list(dict.fromkeys(reasons)),  # each rule once, in match order
        "question": q,
        "instructions": PILOT_UNCLEAR_INSTRUCTIONS if unclear else None,
    }
