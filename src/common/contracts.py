"""Shared data contracts for every pipeline stage (CLAUDE.md section 5).

Each stage reads and writes these shapes. Changing a field here changes the
interface between owners, so it needs a reviewed PR that tags every affected
owner.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal, TypedDict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# --- Closed value sets ------------------------------------------------------

PageType = Literal[
    "guide_page",
    "faq",
    "regulation_summary",
    "advisory_circular",
    "study_guide",
    "toolkit_pdf",
    "data_list",
    "test_injection",
]
PAGE_TYPES: frozenset[str] = frozenset(PageType.__args__)

Audience = Literal["recreational", "part107", "public_safety", "general"]
AUDIENCES: frozenset[str] = frozenset(Audience.__args__)

Strategy = Literal["fixed", "struct"]
STRATEGIES: frozenset[str] = frozenset(Strategy.__args__)

RouterTag = Literal["normal", "location_live", "high_stakes", "pilot_unclear", "injection"]
ROUTER_TAGS: frozenset[str] = frozenset(RouterTag.__args__)

Category = Literal[
    "single_hop", "multi_hop", "comparative", "temporal", "unanswerable", "ambiguous"
]
CATEGORIES: frozenset[str] = frozenset(Category.__args__)

RetrievalMode = Literal["dense", "bm25", "hybrid"]
RETRIEVAL_MODES: frozenset[str] = frozenset(RetrievalMode.__args__)


# --- Stage 1: raw store -----------------------------------------------------


class ManifestEntry(TypedDict):
    """One line of data/raw/manifest.jsonl, written once per fetch."""

    url: str
    final_url: str
    fetched_at: str  # ISO 8601, UTC
    status: int
    content_type: str
    sha256: str  # of the raw bytes
    path: str  # data/raw/{sha256}.bin, relative to the repo root
    robots_allowed: bool


# --- Stage 2: normalized documents ------------------------------------------


class Section(TypedDict):
    heading_path: list[str]
    text: str
    tables: list[str]  # each table as a Markdown table


class Extraction(TypedDict):
    method: str  # e.g. "trafilatura", "bs4", "pdfplumber"
    ocr_used: bool
    warnings: list[str]


class NormalizedDoc(TypedDict):
    """data/normalized/{doc_id}.json"""

    doc_id: str
    source_url: str
    title: str
    page_type: PageType
    audience: Audience
    effective_date: str | None  # YYYY-MM-DD when found on the page
    crawl_ts: str  # ISO 8601
    content_hash: str  # sha256 of the normalized text
    sections: list[Section]
    extraction: Extraction


# --- Stage 3: chunks --------------------------------------------------------


class Chunk(TypedDict):
    """One line of data/chunks/{strategy}@{version}.jsonl"""

    chunk_id: str
    doc_id: str
    strategy: Strategy
    version: str  # e.g. "v1"
    ordinal: int
    text: str
    heading_path: list[str]
    page_type: PageType
    source_url: str
    title: str
    effective_date: str | None
    n_tokens: int
    chunk_hash: str  # sha256 of text


# --- Stages 7 and 8: retrieval and reranking --------------------------------


class Candidate(TypedDict):
    chunk_id: str
    text: str
    source_url: str
    title: str
    effective_date: str | None
    page_type: PageType
    dense_score: float | None
    bm25_score: float | None
    fused_score: float | None


class RerankedCandidate(Candidate):
    rerank_score: float


# --- Stage 9: answers -------------------------------------------------------


class Sentence(TypedDict):
    text: str
    chunk_ids: list[str]


class Citation(TypedDict):
    n: int
    chunk_id: str
    url: str
    title: str
    effective_date: str | None


class Referral(TypedDict):
    kind: Literal["location", "high_stakes"]
    text: str


class Trace(TypedDict):
    candidates: list[Candidate]
    reranked: list[RerankedCandidate]
    gate_score: float | None
    checks: dict


class Latency(TypedDict):
    route: float
    retrieve: float
    rerank: float
    generate: float
    check: float
    total: float


class Answer(TypedDict):
    status: Literal["answered", "refused"]
    sentences: list[Sentence]
    citations: list[Citation]
    referral: Referral | None
    router_tag: RouterTag
    rewritten_question: str | None
    trace: Trace
    latency_ms: Latency
    cost_usd: float


# --- Evaluation -------------------------------------------------------------


class GoldenItem(TypedDict, total=False):
    """One line of eval/golden.jsonl. Written and verified by people only."""

    id: str
    category: Category
    question: str
    gold_answer: str
    # A list (one quote per URL, same order) for multi_hop and comparative items
    # that combine 2+ pages; a single string otherwise.
    source_url: str | list[str]
    evidence_quote: str | list[str]  # each 25 words or fewer, verbatim from its page
    searched_for: str  # unanswerable items only
    author: str
    verified_by: str
    verified_date: str


# --- ID helpers -------------------------------------------------------------

_DEFAULT_PORTS = {"http": 80, "https": 443}
_TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid)$")


def canonical_url(url: str) -> str:
    """Normalize a URL so trivially different spellings get the same doc_id.

    Lowercases scheme and host, drops default ports, fragments, tracking
    parameters and trailing slashes (except the root), and sorts the query.
    """
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"not an absolute URL: {url!r}")
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port and parts.port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING_PARAMS.match(k)
        )
    )
    return urlunsplit((scheme, host, path, query, ""))


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def doc_id_for(url: str) -> str:
    """First 12 hex characters of sha256(canonical_url). Stable across runs."""
    return sha256_hex(canonical_url(url))[:12]


_DOC_ID = re.compile(r"^[0-9a-f]{12}$")
_VERSION = re.compile(r"^v\d+$")
_CHUNK_ID = re.compile(
    r"^(?P<doc_id>[0-9a-f]{12})::(?P<strategy>\w+)@(?P<version>v\d+)::(?P<ordinal>\d{4,})$"
)


def chunk_id_for(doc_id: str, strategy: str, version: str, ordinal: int) -> str:
    """Deterministic chunk ID, e.g. a1b2c3d4e5f6::struct@v1::0007.

    The same normalized text and strategy always give the same IDs, which
    makes upserts idempotent and lets the re-chunk migration find orphans.
    """
    if not _DOC_ID.match(doc_id):
        raise ValueError(f"doc_id must be 12 lowercase hex characters: {doc_id!r}")
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; expected one of {sorted(STRATEGIES)}")
    if not _VERSION.match(version):
        raise ValueError(f"version must look like 'v1': {version!r}")
    if ordinal < 0:
        raise ValueError(f"ordinal must be >= 0: {ordinal}")
    return f"{doc_id}::{strategy}@{version}::{ordinal:04d}"


def parse_chunk_id(chunk_id: str) -> tuple[str, str, str, int]:
    """Inverse of chunk_id_for: returns (doc_id, strategy, version, ordinal)."""
    m = _CHUNK_ID.match(chunk_id)
    if not m or m["strategy"] not in STRATEGIES:
        raise ValueError(f"not a valid chunk_id: {chunk_id!r}")
    return m["doc_id"], m["strategy"], m["version"], int(m["ordinal"])


def strategy_tag(strategy: str, version: str) -> str:
    """'fixed', 'v1' -> 'fixed@v1', the name used in chunk files and configs."""
    return f"{strategy}@{version}"


# --- Light validation (shape checks, not full JSON Schema) ------------------


def _require(obj: dict, keys: list[str], what: str) -> list[str]:
    return [f"{what}: missing '{k}'" for k in keys if k not in obj]


def validate_normalized_doc(doc: dict) -> list[str]:
    """Return a list of problems; empty means the doc matches the contract."""
    errors = _require(doc, list(NormalizedDoc.__annotations__), "doc")
    if errors:
        return errors
    if doc["doc_id"] != doc_id_for(doc["source_url"]):
        errors.append(f"doc {doc['doc_id']}: doc_id does not match source_url")
    if doc["page_type"] not in PAGE_TYPES:
        errors.append(f"doc {doc['doc_id']}: bad page_type {doc['page_type']!r}")
    if doc["audience"] not in AUDIENCES:
        errors.append(f"doc {doc['doc_id']}: bad audience {doc['audience']!r}")
    for i, sec in enumerate(doc["sections"]):
        errors += _require(sec, list(Section.__annotations__), f"doc {doc['doc_id']} section {i}")
    return errors


def validate_chunk(chunk: dict) -> list[str]:
    errors = _require(chunk, list(Chunk.__annotations__), "chunk")
    if errors:
        return errors
    expected = chunk_id_for(chunk["doc_id"], chunk["strategy"], chunk["version"], chunk["ordinal"])
    if chunk["chunk_id"] != expected:
        errors.append(f"chunk {chunk['chunk_id']}: expected id {expected}")
    if chunk["chunk_hash"] != sha256_hex(chunk["text"]):
        errors.append(f"chunk {chunk['chunk_id']}: chunk_hash does not match text")
    if chunk["page_type"] not in PAGE_TYPES:
        errors.append(f"chunk {chunk['chunk_id']}: bad page_type {chunk['page_type']!r}")
    return errors
