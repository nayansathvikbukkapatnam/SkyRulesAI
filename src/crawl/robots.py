"""robots.txt checks, made in code before every request (CLAUDE.md rule 1)."""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import lru_cache
from urllib.parse import unquote, urlsplit
from urllib.robotparser import RobotFileParser


@lru_cache(maxsize=1024)
def _rule_regex(rule_path: str) -> re.Pattern:
    """Compile a robots.txt path rule with Google/RFC 9309 wildcards.

    urllib.robotparser percent-encodes '*' and '$' and then compares by prefix, so
    a rule like `Disallow: /*.json$` would never match anything. Here `*` matches
    any run of characters and a trailing `$` anchors the end.
    """
    path = unquote(rule_path)
    anchored = path.endswith("$")
    if anchored:
        path = path[:-1]
    body = ".*".join(re.escape(part) for part in path.split("*"))
    return re.compile(body + ("$" if anchored else ""))


class RobotsChecker:
    """Fetches robots.txt once per origin and answers can-we-fetch for each URL.

    `fetch(url) -> (status, text)` does the HTTP request, so robots.txt goes through
    the same rate limiter and User-Agent as every other request. When robots.txt
    cannot be read we fail closed (disallow everything), except for a 404 or other
    "not there" 4xx, which by convention means no rules.

    urllib.robotparser parses the file and picks the group for our User-Agent; the
    rule matching is ours: the longest matching rule wins, Allow wins a tie, and
    wildcards are honoured (see _rule_regex).
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
                # RFC 9309: blank lines do not end a group. urllib.robotparser treats
                # a blank line after "User-agent:" as the end of the group and then
                # drops every rule that follows (eCFR's robots.txt is laid out that
                # way), which silently allows everything. Without blank lines, its
                # grouping matches the RFC: a new group starts at a User-agent line
                # that follows rules.
                parser.parse([line for line in text.splitlines() if line.strip()])
            elif status in (401, 403) or status is None or status >= 500:
                parser.disallow_all = True
            else:
                parser.allow_all = True
            self._parsers[origin] = parser
        return self._parsers[origin]

    def _can_fetch(self, parser: RobotFileParser, url: str) -> bool:
        if parser.disallow_all:
            return False
        if parser.allow_all:
            return True
        group = next((e for e in parser.entries if e.applies_to(self.user_agent)), None)
        group = group or parser.default_entry
        if group is None:
            return True
        parts = urlsplit(url)
        target = unquote(parts.path or "/") + (f"?{unquote(parts.query)}" if parts.query else "")
        best_len, allowed = -1, True
        for rule in group.rulelines:
            if not rule.path:  # an empty "Disallow:" allows everything
                continue
            if _rule_regex(rule.path).match(target):
                length = len(unquote(rule.path))
                if length > best_len or (length == best_len and rule.allowance):
                    best_len, allowed = length, rule.allowance
        return allowed

    def allowed(self, url: str) -> bool:
        self.checks += 1
        ok = self._can_fetch(self._parser_for(url), url)
        if not ok:
            self.denied += 1
        return ok

    def crawl_delay(self, url: str) -> float | None:
        delay = self._parser_for(url).crawl_delay(self.user_agent)
        return float(delay) if delay else None
