"""robots.txt checks, made in code before every request (CLAUDE.md rule 1)."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser


class RobotsChecker:
    """Fetches robots.txt once per origin and answers can-we-fetch for each URL.

    `fetch(url) -> (status, text)` does the HTTP request, so robots.txt goes through
    the same rate limiter and User-Agent as every other request. When robots.txt
    cannot be read we fail closed (disallow everything), except for a 404 or other
    "not there" 4xx, which by convention means no rules.
    """

    def __init__(self, user_agent: str, fetch: Callable[[str], tuple[int, str]]) -> None:
        self.user_agent = user_agent
        self._fetch = fetch
        self._parsers: dict[str, RobotFileParser] = {}
        self.checks = 0
        self.denied = 0

    def _parser_for(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._parsers:
            robots_url = f"{origin}/robots.txt"
            parser = RobotFileParser(robots_url)
            try:
                status, text = self._fetch(robots_url)
            except Exception:
                status, text = None, ""
            if status == 200:
                parser.parse(text.splitlines())
            elif status in (401, 403) or status is None or status >= 500:
                parser.disallow_all = True
            else:
                parser.allow_all = True
            self._parsers[origin] = parser
        return self._parsers[origin]

    def allowed(self, url: str) -> bool:
        self.checks += 1
        ok = self._parser_for(url).can_fetch(self.user_agent, url)
        if not ok:
            self.denied += 1
        return ok

    def crawl_delay(self, url: str) -> float | None:
        delay = self._parser_for(url).crawl_delay(self.user_agent)
        return float(delay) if delay else None
