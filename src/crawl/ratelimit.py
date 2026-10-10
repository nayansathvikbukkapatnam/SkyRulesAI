"""Minimum gap between requests to www.faa.gov (CLAUDE.md rule 1: at most 1 per second)."""

from __future__ import annotations

import time
from collections.abc import Callable


class RateLimiter:
    """Blocks so that consecutive requests start at least `min_interval` seconds apart.

    The clock and sleep are injectable so tests can check the gap without waiting.
    `min_gap_observed` is the smallest gap actually measured, reported as crawl evidence.
    """

    def __init__(
        self,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if min_interval <= 0:
            raise ValueError(f"min_interval must be positive: {min_interval}")
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self.min_gap_observed: float | None = None

    def wait(self) -> None:
        """Wait until min_interval has passed since the previous request, then mark this one."""
        now = self._clock()
        if self._last is not None:
            # Loop because a sleep can return a little early on some platforms.
            while (remaining := self.min_interval - (now - self._last)) > 0:
                self._sleep(remaining)
                now = self._clock()
            gap = now - self._last
            if self.min_gap_observed is None or gap < self.min_gap_observed:
                self.min_gap_observed = gap
        self._last = now
