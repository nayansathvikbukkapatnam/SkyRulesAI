"""Demo limits: off by default, per-visitor hourly cap, daily budget, and the server hook."""

import pytest
from fastapi.testclient import TestClient

from src.app.limits import DAY, HOUR, DemoLimits
from src.app.server import create_app
from tests.test_server import CONFIGS, FAKE_LLM, ask, events, fake_answer


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_off_by_default_and_from_an_empty_environment():
    for limits in (DemoLimits(), DemoLimits.from_env({})):
        assert not limits.enabled
        assert all(limits.admit("1.2.3.4") is None for _ in range(100))


def test_from_env_reads_both_limits_and_rejects_bad_values():
    limits = DemoLimits.from_env({"DEMO_QUESTIONS_PER_HOUR": "10", "DEMO_DAILY_BUDGET_USD": "2"})
    assert (limits.per_hour, limits.daily_budget_usd) == (10, 2.0)
    with pytest.raises(ValueError):
        DemoLimits.from_env({"DEMO_QUESTIONS_PER_HOUR": "0"})


def test_hourly_cap_is_per_visitor_and_slides():
    clock = Clock()
    limits = DemoLimits(per_hour=2, clock=clock)
    assert limits.admit("a") is None and limits.admit("a") is None
    assert "2 questions per hour" in limits.admit("a")
    assert limits.admit("b") is None  # another visitor is not affected
    clock.now += HOUR
    assert limits.admit("a") is None


def test_daily_budget_blocks_everyone_until_the_next_day():
    clock = Clock(now=DAY * 100 + 10)
    limits = DemoLimits(daily_budget_usd=0.01, clock=clock)
    assert limits.admit("a") is None
    limits.spend(0.006)
    assert limits.admit("b") is None
    limits.spend(0.006)
    assert "daily limit" in limits.admit("c")
    clock.now = DAY * 101
    assert limits.admit("c") is None


def test_server_blocks_without_running_the_pipeline_and_counts_spend():
    fake = fake_answer()
    limits = DemoLimits(per_hour=1)
    app = create_app(
        answer_fn=fake,
        configs=CONFIGS,
        client=FAKE_LLM,
        retrieve_fn=lambda *a, **k: [],
        rerank_fn=lambda *a, **k: [],
        limits=limits,
    )
    with TestClient(app) as tc:
        first = events(ask(tc, compare=True))
        second = events(ask(tc))
    assert [e for e, _ in first].count("answer") == 2
    assert limits._spent == pytest.approx(0.0002)  # both compare answers are paid for
    assert [e for e, _ in second] == ["error", "done"]
    assert "questions per hour" in second[0][1]["message"]
    assert len(fake.calls) == 2  # the blocked question never reached answer()


def test_visitor_is_the_first_forwarded_address():
    limits = DemoLimits(per_hour=1)
    app = create_app(
        answer_fn=fake_answer(),
        configs=CONFIGS,
        client=FAKE_LLM,
        retrieve_fn=lambda *a, **k: [],
        rerank_fn=lambda *a, **k: [],
        limits=limits,
    )
    with TestClient(app) as tc:
        a = tc.post(
            "/api/ask", json={"question": "q"}, headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1"}
        )
        b = tc.post("/api/ask", json={"question": "q"}, headers={"x-forwarded-for": "8.8.8.8"})
    assert events(a)[-2][0] == "answer" and events(b)[-2][0] == "answer"
    assert set(limits._asked) == {"9.9.9.9", "8.8.8.8"}
