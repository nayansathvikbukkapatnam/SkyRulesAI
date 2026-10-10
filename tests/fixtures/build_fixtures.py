"""Build the unit-test fixtures from the hand-written documents below.

FIXTURES ONLY. These five documents are hand-made test data, not FAA content.
They use the reserved .test domain so they can never be mistaken for the
corpus, and they are excluded from every corpus count. Real sample documents
go in tests/fixtures/real_sample/ once extraction runs (A2).

Run from the repo root:  .venv/bin/python -m tests.fixtures.build_fixtures
"""

from __future__ import annotations

import json
from pathlib import Path

from src.common.contracts import chunk_id_for, doc_id_for, sha256_hex

HERE = Path(__file__).resolve().parent
BASE = "https://fixtures.skyrules.test"
CRAWL_TS = "2026-10-10T09:00:00Z"

DOCS = [
    {
        "path": "/uas/recreational_flyers",
        "title": "[FIXTURE] Recreational Flyers",
        "page_type": "guide_page",
        "audience": "recreational",
        "effective_date": "2025-06-12",
        "sections": [
            (
                ["Recreational Flyers"],
                "Fixture text. Flying for fun follows the recreational rules.",
            ),
            (
                ["Recreational Flyers", "Fly at or below 400 feet"],
                "Fixture text. In uncontrolled airspace, recreational flyers stay at or below "
                "400 feet above ground level.",
            ),
            (
                ["Recreational Flyers", "Take the safety test"],
                "Fixture text. Recreational flyers pass a free online safety test and carry "
                "proof of passing when they fly.",
            ),
        ],
    },
    {
        "path": "/uas/commercial_operators",
        "title": "[FIXTURE] Certificated Remote Pilots",
        "page_type": "guide_page",
        "audience": "part107",
        "effective_date": "2024-11-03",
        "sections": [
            (
                ["Certificated Remote Pilots"],
                "Fixture text. Flying for work or business is covered by the Part 107 rules.",
            ),
            (
                ["Certificated Remote Pilots", "Get a remote pilot certificate"],
                "Fixture text. A Part 107 pilot passes a knowledge test and holds a remote "
                "pilot certificate before flying commercially.",
            ),
        ],
    },
    {
        "path": "/uas/getting_started/register",
        "title": "[FIXTURE] Register Your Drone",
        "page_type": "faq",
        "audience": "general",
        "effective_date": None,
        "sections": [
            (
                ["Register Your Drone", "Who must register"],
                "Fixture text. Drones that weigh more than 0.55 pounds are registered before "
                "they fly. Registration lasts 3 years.",
            ),
        ],
        "tables": {
            0: [
                "| Drone use | Registration | Fee |\n| --- | --- | --- |\n"
                "| Recreational | One number for all your drones | $5 |\n"
                "| Part 107 | One number per drone | $5 per drone |"
            ]
        },
    },
    {
        "path": "/sites/faa.gov/files/uas/fixture_study_guide.pdf",
        "title": "[FIXTURE] Remote Pilot Study Guide",
        "page_type": "study_guide",
        "audience": "part107",
        "effective_date": "2023-02-01",
        "pdf": True,
        "sections": [
            (
                ["Chapter 2 Weather", "Stable and unstable air"],
                "Fixture text. Stable air brings smooth flying and poor visibility; unstable "
                "air brings turbulence and good visibility.",
            ),
        ],
        "tables": {
            0: [
                "| Characteristic | Stable air | Unstable air |\n| --- | --- | --- |\n"
                "| Clouds | Stratiform | Cumuliform |\n| Turbulence | Smooth | Rough |\n"
                "| Visibility | Fair to poor | Good |"
            ]
        },
    },
    {
        "path": "/uas/getting_started/remote_id",
        "title": "[FIXTURE] Remote Identification of Drones",
        "page_type": "regulation_summary",
        "audience": "general",
        "effective_date": "2024-03-16",
        "sections": [
            (
                ["Remote ID", "What Remote ID is"],
                "Fixture text. Remote ID lets a drone in flight share identification and "
                "location information that others can receive.",
            ),
            (
                ["Remote ID", "Ways to comply"],
                "Fixture text. Pilots comply by flying a Standard Remote ID drone, adding a "
                "broadcast module, or flying inside a recognized identification area.",
            ),
        ],
    },
]


def build_doc(spec: dict) -> dict:
    url = BASE + spec["path"]
    tables = spec.get("tables", {})
    sections = [
        {"heading_path": hp, "text": text, "tables": tables.get(i, [])}
        for i, (hp, text) in enumerate(spec["sections"])
    ]
    full_text = "\n\n".join(s["text"] + "".join("\n\n" + t for t in s["tables"]) for s in sections)
    return {
        "doc_id": doc_id_for(url),
        "source_url": url,
        "title": spec["title"],
        "page_type": spec["page_type"],
        "audience": spec["audience"],
        "effective_date": spec["effective_date"],
        "crawl_ts": CRAWL_TS,
        "content_hash": sha256_hex(full_text),
        "sections": sections,
        "extraction": {
            "method": "fixture-pdfplumber" if spec.get("pdf") else "fixture-trafilatura",
            "ocr_used": False,
            "warnings": [],
        },
    }


def _chunk(doc: dict, strategy: str, ordinal: int, text: str, heading_path: list[str]) -> dict:
    return {
        "chunk_id": chunk_id_for(doc["doc_id"], strategy, "v1", ordinal),
        "doc_id": doc["doc_id"],
        "strategy": strategy,
        "version": "v1",
        "ordinal": ordinal,
        "text": text,
        "heading_path": heading_path,
        "page_type": doc["page_type"],
        "source_url": doc["source_url"],
        "title": doc["title"],
        "effective_date": doc["effective_date"],
        "n_tokens": len(text.split()),  # word count stands in for tokens in fixtures
        "chunk_hash": sha256_hex(text),
    }


def build_chunks(docs: list[dict]) -> dict[str, list[dict]]:
    """A tiny stand-in for both chunkers: fixed = one chunk per doc, struct = one per section."""
    fixed, struct = [], []
    for doc in docs:
        body = "\n\n".join(
            s["text"] + "".join("\n\n" + t for t in s["tables"]) for s in doc["sections"]
        )
        fixed.append(_chunk(doc, "fixed", 0, body, []))
        for i, sec in enumerate(doc["sections"]):
            text = " > ".join(sec["heading_path"]) + "\n\n" + sec["text"]
            text += "".join("\n\n" + t for t in sec["tables"])
            struct.append(_chunk(doc, "struct", i, text, sec["heading_path"]))
    return {"fixed@v1": fixed, "struct@v1": struct}


def build_candidates(struct_chunks: list[dict]) -> dict:
    """Sample retrieve() output for one question, with made-up scores."""
    question = "How high can I fly my drone for fun?"
    keys = ("chunk_id", "text", "source_url", "title", "effective_date", "page_type")
    candidates = []
    for rank, chunk in enumerate(struct_chunks[:6]):
        c = {k: chunk[k] for k in keys}
        c["dense_score"] = round(0.82 - 0.06 * rank, 4)
        c["bm25_score"] = round(7.5 - 1.1 * rank, 4)
        c["fused_score"] = round(2 / (60 + rank + 1), 6)
        candidates.append(c)
    return {"question": question, "candidates": candidates}


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def main() -> None:
    docs = [build_doc(spec) for spec in DOCS]
    out = HERE / "normalized"
    out.mkdir(exist_ok=True)
    for old in out.glob("*.json"):
        old.unlink()
    for doc in docs:
        (out / f"{doc['doc_id']}.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        )
    chunks = build_chunks(docs)
    (HERE / "chunks").mkdir(exist_ok=True)
    for tag, rows in chunks.items():
        write_jsonl(HERE / "chunks" / f"{tag}.jsonl", rows)
    (HERE / "candidates.json").write_text(
        json.dumps(build_candidates(chunks["struct@v1"]), indent=2, ensure_ascii=False) + "\n"
    )
    print(f"wrote {len(docs)} docs, {sum(map(len, chunks.values()))} chunks, 1 candidate set")


if __name__ == "__main__":
    main()
