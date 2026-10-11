"""Demo limits for a public host: questions per visitor per hour and spend per day.

Off unless the environment sets them, so a laptop run (make app) has no limits:

    DEMO_QUESTIONS_PER_HOUR=10   per visitor (client IP), over a sliding hour
    DEMO_DAILY_BUDGET_USD=2      LLM spend of all visitors, resets at midnight UTC

A blocked question gets a short message and never reaches the pipeline, so it costs
nothing. Spend comes from each answer's cost_usd, so one question can go slightly over
the budget; the next one is refused.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping

HOUR = 3600.0
DAY = 86400.0


def _number(env: Mapping[str, str], name: str, kind: type) -> int | float | None:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    value = kind(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {raw!r}")
    return value


class DemoLimits:
    def __init__(
        self,
        per_hour: int | None = None,
        daily_budget_usd: float | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.per_hour = per_hour
        self.daily_budget_usd = daily_budget_usd
        self.clock = clock
        self._asked: dict[str, deque[float]] = {}
        self._day = -1
        self._spent = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> DemoLimits:
        return cls(
            per_hour=_number(env, "DEMO_QUESTIONS_PER_HOUR", int),
            daily_budget_usd=_number(env, "DEMO_DAILY_BUDGET_USD", float),
        )

    @property
    def enabled(self) -> bool:
        return self.per_hour is not None or self.daily_budget_usd is not None

    def _roll_day(self, now: float) -> None:
        day = int(now // DAY)
        if day != self._day:
            self._day, self._spent = day, 0.0

    def admit(self, visitor: str) -> str | None:
        """None when the question may run (and it is counted), else the message to show."""
        if not self.enabled:
            return None
        now = self.clock()
        with self._lock:
            self._roll_day(now)
            if self.daily_budget_usd is not None and self._spent >= self.daily_budget_usd:
                return "the public demo reached its daily limit; please try again tomorrow"
            if self.per_hour is not None:
                asked = self._asked.setdefault(visitor, deque())
                while asked and asked[0] <= now - HOUR:
                    asked.popleft()
                if len(asked) >= self.per_hour:
                    return (
                        f"the public demo allows {self.per_hour} questions per hour; "
                        "please try again later"
                    )
                asked.append(now)
            return None

    def spend(self, cost_usd: float | None) -> None:
        if not self.enabled or not cost_usd:
            return
        with self._lock:
            self._roll_day(self.clock())
            self._spent += float(cost_usd)
