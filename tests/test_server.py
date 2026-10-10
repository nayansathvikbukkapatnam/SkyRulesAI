"""Server tests: FastAPI test client, a fake answer(), no models, no network, no keys."""

import json

import pytest
from fastapi.testclient import TestClient

from src.app import server
from src.app.server import MAX_HISTORY_MESSAGES, b3_check_flags, create_app
from src.common.config import load_config
from src.generate.answer import STAGES
from src.generate.claim_check import JUDGE_SYSTEM
from src.generate.llm import LLMResult

CONFIGS = {"b2": load_config("b2"), "b3": load_config("b3")}


def fake_answer(fail_on=None):
    """An answer() stand-in that reports every stage and records its calls."""
    calls = []

    def answer(question, history, config, pilot_type="not_sure", on_stage=None, **kwargs):
        calls.append(
            {
                "question": question,
                "history": history,
                "config": config,
                "pilot_type": pilot_type,
                **kwargs,
            }
        )
        name = "b2" if config is CONFIGS["b2"] else "b3"
        if fail_on == name:
            raise RuntimeError(f"{name} pipeline failed")
        for stage in STAGES:
            on_stage(stage, "started", 0.0)
            on_stage(stage, "done", 12.5)
        return {
            "status": "answered",
            "sentences": [{"text": f"{name} sentence.", "chunk_ids": ["c1"]}],
            "citations": [],
            "referral": None,
            "router_tag": "normal",
            "rewritten_question": None,
            "trace": {"reranked": [{"chunk_id": "c1", "text": "passage"}]},
            "latency_ms": {},
            "cost_usd": 0.0001,
        }

    answer.calls = calls
    return answer


FAKE_LLM = object()  # stands in for the LLM client; the fake answer() never calls it


def make_client(answer_fn, client=FAKE_LLM):
    app = create_app(
        answer_fn=answer_fn,
        configs=CONFIGS,
        client=client,
        retrieve_fn=lambda *a, **k: [],
        rerank_fn=lambda *a, **k: [],
    )
    return TestClient(app)


def events(response):
    """Parse a server-sent event stream into [(event, data)]."""
    out = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def ask(tc, **body):
    return tc.post("/api/ask", json={"question": "How high can I fly?", **body})


# --- The event stream ---------------------------------------------------------------------


def test_stage_events_then_one_answer_then_done():
    with make_client(fake_answer()) as tc:
        r = ask(tc)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    ev = events(r)
    stages = [(d["name"], d["status"]) for e, d in ev if e == "stage"]
    assert stages == [(s, status) for s in STAGES for status in ("started", "done")]
    assert all(d["config"] == "b3" for e, d in ev if e == "stage")
    assert [e for e, _ in ev][-2:] == ["answer", "done"]
    answer = ev[-2][1]
    assert answer["config"] == "b3" and answer["status"] == "answered"
    assert "b3_check_flags" not in answer


def test_request_fields_reach_answer_with_the_loaded_dependencies():
    fake = fake_answer()
    retrieve, rerank, llm = (lambda *a, **k: []), (lambda *a, **k: []), object()
    app = create_app(
        answer_fn=fake, configs=CONFIGS, client=llm, retrieve_fn=retrieve, rerank_fn=rerank
    )
    history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    with TestClient(app) as tc:
        ask(tc, history=history, pilot_type="part107")
    call = fake.calls[0]
    assert call["question"] == "How high can I fly?"
    assert call["history"] == history
    assert call["pilot_type"] == "part107"
    assert call["config"] is CONFIGS["b3"]
    assert (call["client"], call["retrieve_fn"], call["rerank_fn"]) == (llm, retrieve, rerank)


def test_history_is_trimmed_to_the_last_six_turns():
    fake = fake_answer()
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(20)
    ]
    with make_client(fake) as tc:
        ask(tc, history=history)
    sent = fake.calls[0]["history"]
    assert len(sent) == MAX_HISTORY_MESSAGES
    assert sent[-1]["content"] == "m19"


def test_compare_sends_b3_then_b2_with_flags(monkeypatch):
    monkeypatch.setattr(server, "b3_check_flags", lambda b2, cfg, client: [["No citation"]])
    with make_client(fake_answer()) as tc:
        ev = events(ask(tc, compare=True))
    answers = [d for e, d in ev if e == "answer"]
    assert [a["config"] for a in answers] == ["b3", "b2"]
    assert answers[1]["b3_check_flags"] == [["No citation"]]
    assert {d["config"] for e, d in ev if e == "stage"} == {"b3", "b2"}
    assert ev[-1][0] == "done"


def test_compare_still_sends_b2_when_the_flags_fail(monkeypatch):
    def broken(*args):
        raise TimeoutError("judge timed out")

    monkeypatch.setattr(server, "b3_check_flags", broken)
    with make_client(fake_answer()) as tc:
        ev = events(ask(tc, compare=True))
    b2 = [d for e, d in ev if e == "answer"][1]
    assert b2["b3_check_flags"] is None
    assert b2["b3_check_error"] == "TimeoutError: judge timed out"


# --- Errors ------------------------------------------------------------------------------


def test_answer_exception_becomes_an_error_event_and_no_answer():
    with make_client(fake_answer(fail_on="b3")) as tc:
        ev = events(ask(tc))
    assert [e for e, _ in ev] == ["error", "done"]
    assert ev[0][1]["message"] == "RuntimeError: b3 pipeline failed"


def test_compare_keeps_the_b3_answer_when_b2_fails():
    with make_client(fake_answer(fail_on="b2")) as tc:
        ev = events(ask(tc, compare=True))
    kinds = [e for e, _ in ev if e != "stage"]
    assert kinds == ["answer", "error", "done"]


def test_missing_llm_client_is_reported_not_fatal(monkeypatch):
    class Unavailable:
        def __init__(self, cfg):
            raise RuntimeError("LLM_PROVIDER must be one of ['anthropic', 'bedrock']")

    monkeypatch.setattr("src.generate.llm.LLMClient", Unavailable)
    with make_client(fake_answer(), client=None) as tc:
        health = tc.get("/api/health").json()
        ev = events(ask(tc))
    assert health["status"] == "degraded" and health["llm"]["ready"] is False
    assert "LLM_PROVIDER" in health["llm"]["error"]
    assert ev[0][0] == "error" and "not available" in ev[0][1]["message"]


@pytest.mark.parametrize(
    "body",
    [
        {"question": ""},
        {"question": "q", "pilot_type": "astronaut"},
        {"question": "q", "history": [{"role": "system", "content": "x"}]},
        {"question": "q" * 1001},
        {},
    ],
    ids=["empty", "bad-pilot-type", "bad-role", "too-long", "missing"],
)
def test_invalid_requests_are_rejected(body):
    fake = fake_answer()
    with make_client(fake) as tc:
        r = tc.post("/api/ask", json=body)
    assert r.status_code == 422 and fake.calls == []


# --- Health and the page -----------------------------------------------------------------


def test_health_reports_what_is_loaded_and_never_a_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-value-for-the-health-test")
    with make_client(fake_answer()) as tc:
        r = tc.get("/api/health")
    body = r.json()
    assert body["status"] == "ok"
    assert body["configs"] == {"main": "b3", "compare": "b2"}
    assert set(body["index"]["bm25"]) == {"fixed@v1", "struct@v1"}
    assert isinstance(body["index"]["chroma"], bool)
    assert "fake-key-value" not in r.text


def test_root_serves_the_page():
    with make_client(fake_answer()) as tc:
        r = tc.get("/")
    assert r.status_code == 200 and "SkyRules AI" in r.text


# --- B3 checks over the B2 answer (UI_SPEC 4.7) ------------------------------------------


class Judge:
    def __init__(self, verdict):
        self.verdict = verdict

    def complete(self, system, messages, max_tokens=None):
        assert system == JUDGE_SYSTEM
        return LLMResult(
            text=json.dumps({"supported": self.verdict, "reason": "r"}),
            parsed_json=None,
            input_tokens=10,
            output_tokens=5,
            latency_ms=1,
            cost_usd=0.0,
            provider="anthropic",
            model_id="claude-haiku-5-5",
            stop_reason="end_turn",
            truncated=False,
            temperature=None,
            thinking="disabled",
            effort="medium",
            cached=False,
        )


PASSAGE = "Recreational flyers stay at or below 400 feet in uncontrolled airspace."
LONG = " ".join(f"w{i}" for i in range(40))


def b2_answer(sentences):
    return {
        "status": "answered",
        "sentences": sentences,
        "trace": {
            "reranked": [{"chunk_id": "c1", "text": PASSAGE}, {"chunk_id": "c2", "text": LONG}]
        },
    }


def test_b3_flags_name_each_failed_check_per_sentence():
    b2 = b2_answer(
        [
            {"text": "Recreational flyers stay at or below 400 feet.", "chunk_ids": ["c1"]},
            {"text": "This one cites nothing.", "chunk_ids": []},
            {"text": "Flyers may go up to 500 feet.", "chunk_ids": ["c1"]},
            {"text": " ".join(f"w{i}" for i in range(26)) + ".", "chunk_ids": ["c2"]},
        ]
    )
    flags = b3_check_flags(b2, CONFIGS["b3"], Judge("yes"))
    assert flags[0] == []
    assert flags[1] == ["No citation"]
    assert flags[2] == ["Number not found in source"]
    assert flags[3] == ["Quote over 25 words"]


def test_b3_flags_include_unsupported_claims():
    b2 = b2_answer(
        [{"text": "Recreational flyers stay at or below 400 feet.", "chunk_ids": ["c1"]}]
    )
    assert b3_check_flags(b2, CONFIGS["b3"], Judge("no")) == [
        ["Claim not supported by cited passage"]
    ]


def test_no_flags_for_a_refused_b2_answer():
    assert b3_check_flags({"status": "refused", "sentences": []}, CONFIGS["b3"], Judge("yes")) == []
