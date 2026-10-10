"""Crawl the allowed drone-rule sources once into data/raw/ (CLAUDE.md rule 1, task A1).

The sites and paths come from configs/crawl/sources.yaml (see src/crawl/sources.py).
Etiquette is enforced here in code, not by convention:
- only the listed hosts and paths, never the PII/login/contact paths they exclude;
- robots.txt is checked before every request, redirect hops included, with
  wildcard rules honoured;
- at least 1.0 s between requests across all sites (longer if a robots.txt sets
  a Crawl-delay);
- a User-Agent carrying the team contact email from .env;
- exponential backoff on 429 and 5xx;
- raw bytes are saved to data/raw/{sha256}.bin BEFORE any parsing, then one
  manifest line is appended.

Run with `make crawl` (or `python -m src.crawl.crawler`). It refuses to run when a
manifest with 150+ documents already exists unless --force is given, and it never
re-fetches a URL that is already in the manifest (resume). `--per-source 5` does a
small dry run whose pages are then reused by the full crawl.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.common import config, paths
from src.common.contracts import ManifestEntry, canonical_url, sha256_hex
from src.crawl.ratelimit import RateLimiter
from src.crawl.robots import RobotsChecker
from src.crawl.sources import CRAWL, SKIP, SourceSet

log = logging.getLogger(__name__)

USER_AGENT_TEMPLATE = "SkyRulesAI-MSAI633 (+{email})"

MAX_DEPTH = 3
MIN_INTERVAL_S = 1.0
TIMEOUT_S = 30.0
MAX_ATTEMPTS = 4
MAX_REDIRECTS = 5
MAX_SITEMAPS_PER_SOURCE = 50
REFUSE_AT = 150

HTML, PDF = "text/html", "application/pdf"
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


def user_agent_for(email: str) -> str:
    return USER_AGENT_TEMPLATE.format(email=email)


def content_kind(resp, url: str) -> str | None:
    """HTML or PDF from the Content-Type header (or PDF magic bytes); None for anything else."""
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype == PDF or resp.content[:5] == b"%PDF-":
        return PDF
    if ctype in (HTML, "application/xhtml+xml"):
        return HTML
    return None


def extract_links(body: bytes, base: str, sources: SourceSet) -> list[str]:
    """Canonical URLs of every <a href> on an HTML page, in page order, deduped."""
    soup = BeautifulSoup(body, "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        link = sources.normalize(a["href"], base)
        if link and link not in seen:
            seen.add(link)
            links.append(link)
    return links


def read_manifest(path: Path) -> list[dict]:
    """All manifest lines. A torn last line (crash mid-write) is skipped with a warning."""
    if not path.exists():
        return []
    entries = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("manifest line %d is not valid JSON; ignoring it", n)
    return entries


def should_refuse(manifest_path: Path, force: bool) -> bool:
    """Crawl once: refuse when a full manifest already exists, unless forced."""
    return not force and len(read_manifest(manifest_path)) >= REFUSE_AT


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _repo_relative(path: Path) -> str:
    path = path.resolve()
    try:
        return path.relative_to(paths.ROOT).as_posix()
    except ValueError:
        return path.as_posix()


class RetryableStatus(Exception):
    """A 429 or 5xx response, retried with backoff."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class Crawler:
    """Breadth-first crawl of the allowed sources with resume from the manifest.

    `session` only needs `.get(url, headers=, timeout=, allow_redirects=)`, so tests
    pass a fake site and never touch the network.
    """

    def __init__(
        self,
        session,
        user_agent: str,
        sources: SourceSet,
        *,
        raw_dir: Path = paths.RAW,
        manifest_path: Path = paths.MANIFEST,
        skipped_path: Path = paths.SKIPPED,
        max_urls: int | None = None,
        max_depth: int = MAX_DEPTH,
        limiter: RateLimiter | None = None,
        sleep=time.sleep,
        retry_wait=None,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self.session = session
        self.user_agent = user_agent
        self.sources = sources
        self.raw_dir = raw_dir
        self.manifest_path = manifest_path
        self.skipped_path = skipped_path
        self.max_urls = max_urls if max_urls is not None else sources.max_urls_total
        self.max_depth = max_depth
        self.timeout = timeout
        self.limiter = limiter or RateLimiter(MIN_INTERVAL_S, sleep=sleep)
        self._sleep = sleep
        self._retry_wait = (
            retry_wait if retry_wait is not None else wait_exponential(multiplier=2, min=2, max=60)
        )
        self.robots = RobotsChecker(user_agent, self._fetch_robots_txt)
        self.stats: Counter[str] = Counter()
        self.saved_by_source: Counter[str] = Counter()
        self.skipped: Counter[str] = Counter()
        self._logged_skips: set[str] = set()

    # --- HTTP -----------------------------------------------------------------

    def _get(self, url: str):
        """One GET (no redirects), rate limited, retried on 429, 5xx and network errors."""
        retrying = Retrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=self._retry_wait,
            retry=retry_if_exception_type(
                (RetryableStatus, requests.ConnectionError, requests.Timeout)
            ),
            sleep=self._sleep,
            reraise=True,
        )
        for attempt in retrying:
            with attempt:
                self.limiter.wait()
                self.stats["requests"] += 1
                resp = self.session.get(
                    url,
                    headers={"User-Agent": self.user_agent},
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise RetryableStatus(resp.status_code)
                return resp
        raise AssertionError("unreachable: tenacity reraises the last error")

    def _fetch_robots_txt(self, url: str) -> tuple[int, str]:
        current = url
        for _ in range(MAX_REDIRECTS):
            try:
                resp = self._get(current)
            except RetryableStatus as e:
                return e.status, ""
            location = resp.headers.get("Location")
            if resp.status_code in REDIRECT_CODES and location:
                current = urljoin(current, location)
                continue
            return resp.status_code, resp.content.decode("utf-8", errors="replace")
        return 599, ""  # too many redirects: treated as unreadable, so disallow all

    def _robots_allowed(self, url: str) -> bool:
        allowed = self.robots.allowed(url)
        delay = self.robots.crawl_delay(url)
        if delay and delay > self.limiter.min_interval:
            self.limiter.min_interval = delay
        return allowed

    def _fetch(self, url: str, in_scope_only: bool = True):
        """Fetch a URL, following redirects by hand so every hop is checked.

        Returns (response, final canonical URL, None) or (None, URL, skip reason).
        With in_scope_only=False (sitemaps) a redirect must stay on the same host.
        """
        current = url
        host = urlsplit(url).hostname
        for _ in range(MAX_REDIRECTS + 1):
            if not self._robots_allowed(current):
                return None, canonical_url(current), "robots_disallowed"
            try:
                resp = self._get(current)
            except RetryableStatus as e:
                return None, canonical_url(current), f"http_{e.status}_after_retries"
            except requests.RequestException:
                return None, canonical_url(current), "network_error"
            location = resp.headers.get("Location")
            if resp.status_code not in REDIRECT_CODES or not location:
                return resp, canonical_url(current), None
            nxt = self.sources.request_url(location, current)
            if nxt is None:
                return None, canonical_url(current), "bad_redirect"
            target = canonical_url(nxt)
            if in_scope_only:
                kind, reason, _ = self.sources.scope(target)
                if kind == SKIP:
                    return None, target, f"redirect_{reason}"
            elif urlsplit(target).hostname != host:
                return None, target, "redirect_other_host"
            current = nxt
        return None, canonical_url(current), "too_many_redirects"

    # --- Storage --------------------------------------------------------------

    def _skip(self, url: str, reason: str, depth: int | None, found_on: str | None) -> None:
        """Log a skipped URL once per run to skipped.jsonl, with its reason."""
        if url in self._logged_skips:
            return
        self._logged_skips.add(url)
        self.skipped[reason] += 1
        self.skipped_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"url": url, "reason": reason, "depth": depth, "found_on": found_on, "at": _now()}
        with self.skipped_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _save(self, url: str, final_url: str, resp, kind: str) -> ManifestEntry:
        """Write the raw bytes first, then append the manifest line (rule 1)."""
        sha = sha256_hex(resp.content)
        path = self.raw_dir / f"{sha}.bin"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(resp.content)
            os.replace(tmp, path)
        entry = ManifestEntry(
            url=url,
            final_url=final_url,
            fetched_at=_now(),
            status=resp.status_code,
            content_type=kind,
            sha256=sha,
            path=_repo_relative(path),
            robots_allowed=True,
        )
        self._append_manifest(entry)
        return entry

    def _append_manifest(self, entry: ManifestEntry) -> None:
        with self.manifest_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    @staticmethod
    def _read_raw(entry: dict) -> bytes | None:
        path = Path(entry["path"])
        if not path.is_absolute():
            path = paths.ROOT / path
        return path.read_bytes() if path.exists() else None

    # --- Crawl ----------------------------------------------------------------

    def sitemap_seeds(self) -> list[str]:
        """In-scope URLs listed in each source's sitemap (and its child sitemaps)."""
        seeds: list[str] = []
        for source in self.sources.sources:
            if not source.sitemap:
                continue
            pending, visited = [source.sitemap], set()
            while pending and len(visited) < MAX_SITEMAPS_PER_SOURCE:
                sitemap = pending.pop(0)
                if sitemap in visited:
                    continue
                visited.add(sitemap)
                resp, _, reason = self._fetch(sitemap, in_scope_only=False)
                if reason is None and resp.status_code != 200:
                    reason = f"http_{resp.status_code}"
                if reason:
                    self._skip(sitemap, f"sitemap_{reason}", None, None)
                    continue
                try:
                    root = ET.fromstring(resp.content)
                except ET.ParseError:
                    self._skip(sitemap, "sitemap_parse_error", None, None)
                    continue
                locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
                if root.tag.endswith("sitemapindex"):
                    children = (self.sources.request_url(x, sitemap) for x in locs)
                    pending += [u for u in children if u and urlsplit(u).hostname == source.host]
                    continue
                for loc in locs:
                    self._add_seed(seeds, self.sources.normalize(loc, sitemap), source.name)
        return seeds

    def index_seeds(self) -> list[str]:
        """Seeds listed on each source's index pages (e.g. eCFR section numbers).

        Like sitemaps, index pages are read for discovery only and never saved;
        each item they list is fetched and saved as its own document.
        """
        seeds: list[str] = []
        for source in self.sources.sources:
            for index_url in source.index_urls:
                resp, _, reason = self._fetch(index_url, in_scope_only=False)
                if reason is None and resp.status_code != 200:
                    reason = f"http_{resp.status_code}"
                if reason:
                    self._skip(index_url, f"index_{reason}", None, None)
                    continue
                text = resp.content.decode("utf-8", errors="replace")
                for match in source.index_pattern.finditer(text):
                    url = source.index_template.format(*match.groups())
                    self._add_seed(seeds, self.sources.normalize(url, index_url), source.name)
        return seeds

    def _add_seed(self, seeds: list[str], link: str | None, source_name: str) -> None:
        """Keep a discovered URL only if it is in scope for the source that listed it."""
        if link and link not in seeds:
            kind, _, name = self.sources.scope(link)
            if kind != SKIP and name == source_name:
                seeds.append(link)

    def _source_name(self, url: str) -> str | None:
        source = self.sources.source_for(url)
        return source.name if source else None

    def run(self, seeds: list[str]) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        entries = read_manifest(self.manifest_path)
        done: dict[str, dict] = {}
        for e in entries:
            done[e["url"]] = e
            done.setdefault(e["final_url"], e)
        total = len(entries)
        per_source = Counter(self._source_name(e["url"]) for e in entries)
        caps = {s.name: s.max_urls for s in self.sources.sources}

        queue: deque[tuple[str, int, str | None]] = deque()
        seen: set[str] = set()

        def enqueue(url: str, depth: int, found_on: str | None) -> None:
            if url in seen:
                return
            seen.add(url)
            kind, reason, _ = self.sources.scope(url)
            if kind == SKIP:
                self._skip(url, reason, depth, found_on)
            elif depth > self.max_depth:
                self._skip(url, "max_depth", depth, found_on)
            else:
                queue.append((url, depth, found_on))

        for seed in seeds:
            link = self.sources.normalize(seed, seed)
            if link:
                enqueue(link, 0, None)

        while queue:
            url, depth, found_on = queue.popleft()
            if url in done:
                # Resume: never re-fetch, but follow links from the saved bytes.
                entry = done[url]
                self.stats["resumed"] += 1
                body, final_url, kind = (
                    self._read_raw(entry),
                    entry["final_url"],
                    entry["content_type"],
                )
            else:
                name = self._source_name(url)
                if total >= self.max_urls:
                    self._skip(url, "max_urls", depth, found_on)
                    continue
                if per_source[name] >= caps[name]:
                    self._skip(url, "max_urls_for_source", depth, found_on)
                    continue
                total += 1
                per_source[name] += 1
                resp, final_url, reason = self._fetch(url)
                if reason is None and final_url != url and final_url in done:
                    reason = "duplicate_of_fetched_url"
                if reason is None and resp.status_code != 200:
                    reason = f"http_{resp.status_code}"
                kind = content_kind(resp, final_url) if reason is None else None
                if reason is None and kind is None:
                    reason = "unsupported_content_type"
                if reason:
                    self._skip(url, reason, depth, found_on)
                    continue
                entry = self._save(url, final_url, resp, kind)
                done[url] = entry
                done.setdefault(final_url, entry)
                body = resp.content
                self.stats["saved"] += 1
                self.saved_by_source[name] += 1
                log.info("[%d] %s %s depth=%d %s", self.stats["saved"], name, kind, depth, url)

            if kind == HTML and body is not None and self.sources.scope(final_url)[0] == CRAWL:
                for link in extract_links(body, final_url, self.sources):
                    enqueue(link, depth + 1, url)


def crawl_stats(crawler: Crawler, started_at: str, n_seeds: int) -> dict:
    """Evidence for reports/01_ingestion.md, written to reports/artifacts/crawl_stats.json."""
    entries = read_manifest(crawler.manifest_path)
    gap = crawler.limiter.min_gap_observed
    by_source: dict[str, Counter] = {}
    for e in entries:
        name = crawler._source_name(e["url"]) or "unknown"
        by_source.setdefault(name, Counter())[e["content_type"]] += 1
    return {
        "command": "make crawl",
        "started_at": started_at,
        "finished_at": _now(),
        # The real address is in .env; the artifact records only where it comes from.
        "user_agent": user_agent_for("<CRAWL_CONTACT_EMAIL>"),
        "sources": {
            s.name: {"host": s.host, "max_urls": s.max_urls} for s in crawler.sources.sources
        },
        "limits": {
            "max_urls_total": crawler.max_urls,
            "max_depth": crawler.max_depth,
            "min_interval_s": MIN_INTERVAL_S,
            "effective_min_interval_s": crawler.limiter.min_interval,
            "max_attempts": MAX_ATTEMPTS,
        },
        "requests_this_run": crawler.stats["requests"],
        "min_gap_between_requests_s": round(gap, 3) if gap is not None else None,
        "robots": {"checks": crawler.robots.checks, "disallowed": crawler.robots.denied},
        "seeds": n_seeds,
        "saved_this_run": crawler.stats["saved"],
        "saved_this_run_by_source": dict(crawler.saved_by_source),
        "resumed_from_manifest": crawler.stats["resumed"],
        "manifest": {
            "entries": len(entries),
            "unique_sha256": len({e["sha256"] for e in entries}),
            "by_content_type": dict(Counter(e["content_type"] for e in entries)),
            "by_source": {k: dict(v) for k, v in sorted(by_source.items())},
        },
        "skipped_this_run_by_reason": dict(sorted(crawler.skipped.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crawl the allowed drone-rule sources once.")
    parser.add_argument("--force", action="store_true", help="run even if a full manifest exists")
    parser.add_argument("--sources", help="comma-separated source names (default: all enabled)")
    parser.add_argument("--per-source", type=int, help="cap every source at N URLs (dry run)")
    parser.add_argument("--max-urls", type=int, help="total URL cap (default from sources.yaml)")
    parser.add_argument("--max-depth", type=int, default=MAX_DEPTH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if should_refuse(paths.MANIFEST, args.force):
        print(
            f"refusing to crawl: {paths.MANIFEST} already has {REFUSE_AT}+ entries "
            "(crawl once; use FORCE=1 / --force only if the team agreed)"
        )
        return 1

    only = args.sources.split(",") if args.sources else None
    sources = SourceSet.load(only=only, cap=args.per_source)
    email = config.env("CRAWL_CONTACT_EMAIL", required=True)
    started_at = _now()
    with requests.Session() as session:
        crawler = Crawler(
            session,
            user_agent_for(email),
            sources,
            max_urls=args.max_urls,
            max_depth=args.max_depth,
        )
        seeds = [u for s in sources.sources for u in s.seeds]
        seeds += crawler.sitemap_seeds() + crawler.index_seeds()
        log.info("%d seeds (configured + sitemaps + index pages)", len(seeds))
        crawler.run(seeds)

    stats = crawl_stats(crawler, started_at, len(seeds))
    paths.ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = paths.ARTIFACTS / "crawl_stats.json"
    out.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(stats, indent=2))
    print(f"wrote {_repo_relative(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
