"""Query log tests: one JSON line per answer, named fields only, never raises."""

import json
import threading

from src.generate import log as log_mod
from src.generate.log import RAW_TEXT_LIMIT, build_record, log_query

ANSWERED = {
    "status": "answered",
    "sentences": [{"text": "Fly at or below 400 feet.", "chunk_ids": ["c1"]}],
    "citations": [{"n": 1, "chunk_id": "c1", "url": "https://example.test/a", "title": "A"}],
    "referral": {"kind": "location", "text": "Check B4UFLY."},
    "router_tag": "location_live",
    "rewritten_question": "How high can I fly near San Diego?",
    "trace": {
        "candidates": [
            {
                "chunk_id": "c1",
                "text": "long passage text",
                "dense_score": 0.8,
                "bm25_score": 7.0,
                "fused_score": 0.03,
            },
            {
                "chunk_id": "c2",
                "text": "other text",
                "dense_score": 0.5,
                "bm25_score": None,
                "fused_score": 0.01,
            },
        ],
        "reranked": [{"chunk_id": "c1", "text": "long passage text", "rerank_score": 4.2}],
        "gate_score": 4.2,
        "checks": {
            "ok": True,
            "requested": ["citation_validity", "quote_limit"],
            "per_check": {"citation_validity": True, "quote_limit": True},
            "sentences": [{"text": "Fly at or below 400 feet.", "chunk_ids": ["c1"], "ok": True}],
        },
        "refusal_reason": None,
        "stopped_at": None,
        "route_reasons": ["location:san diego"],
        "generation": {
            "status": "answered",
            "error": None,
            "raw_text": "x" * 5000,
            "usage": {
                "input_tokens": 900,
                "output_tokens": 40,
                "cost_usd": 0.0001,
                "latency_ms": 800,
            },
        },
    },
    "latency_ms": {
        "route": 1,
        "retrieve": 50,
        "rerank": 120,
        "generate": 800,
        "check": 900,
        "total": 1871,
    },
    "cost_usd": 0.0002,
}

REFUSED = {
    "status": "refused",
    "sentences": [],
    "citations": [],
    "referral": None,
    "router_tag": "normal",
    "rewritten_question": None,
    "trace": {
        "candidates": [],
        "reranked": [],
        "gate_score": 0.1,
        "checks": {},
        "refusal_reason": "evidence gate: no passage scored above the threshold",
        "stopped_at": "generate",
        "route_reasons": [],
    },
    "latency_ms": {"total": 60},
    "cost_usd": 0.0,
}


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_one_json_line_per_call_with_the_report_fields(tmp_path):
    path = tmp_path / "logs" / "queries.jsonl"  # the folder is created on first write
    log_query(
        "How high near San Diego?", ANSWERED, config_name="b3", pilot_type="part107", path=path
    )
    (r,) = lines(path)
    assert r["question"] == "How high near San Diego?"
    assert r["rewritten_question"] == "How high can I fly near San Diego?"
    assert (r["config"], r["pilot_type"], r["router_tag"]) == ("b3", "part107", "location_live")
    assert r["route_reasons"] == ["location:san diego"]
    assert r["status"] == "answered" and r["referral_kind"] == "location"
    assert r["candidates"] == [
        {"chunk_id": "c1", "dense_score": 0.8, "bm25_score": 7.0, "fused_score": 0.03},
        {"chunk_id": "c2", "dense_score": 0.5, "bm25_score": None, "fused_score": 0.01},
    ]
    assert r["reranked"] == [{"chunk_id": "c1", "rerank_score": 4.2}]
    assert r["gate_score"] == 4.2
    assert r["checks"]["per_check"] == {"citation_validity": True, "quote_limit": True}
    assert r["checks"]["sentences"] == [{"chunk_ids": ["c1"], "ok": True}]
    assert r["citations"] == ["c1"]
    assert r["latency_ms"]["total"] == 1871 and r["cost_usd"] == 0.0002
    assert r["generation"]["usage"]["input_tokens"] == 900
    assert r["ts"].endswith("+00:00")


def test_passage_text_is_not_copied_into_the_log():
    record = build_record("q", ANSWERED)
    text = json.dumps(record)
    assert "long passage text" not in text and "other text" not in text


def test_raw_reply_is_cut_to_the_limit():
    assert len(build_record("q", ANSWERED)["generation"]["raw_text"]) == RAW_TEXT_LIMIT


def test_refusal_records_the_reason_and_stage(tmp_path):
    path = tmp_path / "q.jsonl"
    log_query("Which drone should I buy?", REFUSED, config_name="b3", path=path)
    (r,) = lines(path)
    assert r["status"] == "refused"
    assert r["refusal_reason"].startswith("evidence gate")
    assert r["stopped_at"] == "generate"
    assert r["generation"] is None and r["sentences"] == []


def test_lines_are_appended(tmp_path):
    path = tmp_path / "q.jsonl"
    log_query("first", ANSWERED, path=path)
    log_query("second", REFUSED, path=path)
    assert [r["question"] for r in lines(path)] == ["first", "second"]


def test_default_path_is_the_module_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(log_mod, "QUERY_LOG", tmp_path / "default.jsonl")
    log_query("q", ANSWERED)
    assert len(lines(tmp_path / "default.jsonl")) == 1


def test_write_failure_does_not_raise(tmp_path, caplog):
    blocked = tmp_path / "a-file"
    blocked.write_text("not a folder")
    log_query("q", ANSWERED, path=blocked / "queries.jsonl")  # parent is a file: cannot write
    assert "could not write the query log" in caplog.text


def test_concurrent_writes_give_whole_lines(tmp_path):
    path = tmp_path / "q.jsonl"
    threads = [
        threading.Thread(target=log_query, args=(f"q{i}", ANSWERED), kwargs={"path": path})
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    records = lines(path)  # every line parses as JSON
    assert sorted(r["question"] for r in records) == sorted(f"q{i}" for i in range(20))


def test_no_credential_reaches_the_log(tmp_path, monkeypatch):
    secret = "fake-key-value-for-the-log-test"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("AWS_PROFILE", "fake-profile-for-the-log-test")
    result = {**ANSWERED, "api_key": secret, "headers": {"x-api-key": secret}}  # unexpected extras
    path = tmp_path / "q.jsonl"
    log_query("q", result, path=path)
    text = path.read_text()
    assert secret not in text and "fake-profile" not in text
    assert "api_key" not in text and "headers" not in text
