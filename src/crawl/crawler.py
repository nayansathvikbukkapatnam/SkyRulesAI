"""Crawl the FAA drone section once into data/raw/ (CLAUDE.md rule 1, task A1).

Etiquette is enforced here in code, not by convention:
- only www.faa.gov, only the drone paths below, never the PII paths in EXCLUDED;
- robots.txt is checked before every request, redirect hops included;
- at least 1.0 s between requests (longer if robots.txt sets a Crawl-delay);
- a User-Agent carrying the team contact email from .env;
- exponential backoff on 429 and 5xx;
- raw bytes are saved to data/raw/{sha256}.bin BEFORE any parsing, then one
  manifest line is appended.

Run with `make crawl` (or `python -m src.crawl.crawler`). It refuses to run when a
manifest with 150+ documents already exists unless --force is given, and it never
re-fetches a URL that is already in the manifest (resume).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.common import config, paths
from src.common.contracts import ManifestEntry, canonical_url, sha256_hex
from src.crawl.ratelimit import RateLimiter
from src.crawl.robots import RobotsChecker

log = logging.getLogger(__name__)

HOST = "www.faa.gov"
START_URL = "https://www.faa.gov/uas"
SITEMAP_URL = "https://www.faa.gov/sitemap.xml"
USER_AGENT_TEMPLATE = "SkyRulesAI-MSAI633 (+{email})"

MAX_URLS = 400
MAX_DEPTH = 3
MIN_INTERVAL_S = 1.0
TIMEOUT_S = 30.0
MAX_ATTEMPTS = 4
MAX_REDIRECTS = 5
MAX_SITEMAPS = 50
REFUSE_AT = 150

# Pages under these prefixes are fetched and their links followed.
CRAWL_PREFIXES = ("/uas/", "/sites/faa.gov/files/uas/")
# Drone advisory circulars and PDFs elsewhere on the same host. Links are only
# ever taken from drone-section pages, so reaching one of these means a /uas/
# page linked to it. They are fetched but their own links are not followed.
LEAF_PREFIXES = (
    "/regulations_policies/advisory_circulars/",
    "/documentlibrary/media/advisory_circular/",
)
# Checked before anything else, so an excluded path is never requested (rule 2: no PII).
EXCLUDED = (
    ("contact_page", re.compile(r"/contact")),
    ("waivers_issued", re.compile(r"waivers?_issued")),
    ("sightings", re.compile(r"sighting")),
    ("spanish", re.compile(r"^/uas/espanol(/|$)")),
    ("search", re.compile(r"^/search(/|$)")),
    ("user_account", re.compile(r"^/user(/|$)")),
    ("login", re.compile(r"log_?in|sign_?in")),
)
SKIP_EXTENSIONS = frozenset(
    ".jpg .jpeg .png .gif .svg .webp .ico .bmp .tif .tiff .mp4 .mov .avi .mp3 .wav "
    ".zip .gz .css .js .xml .json .rss .doc .docx .xls .xlsx .ppt .pptx .kml .kmz".split()
)

HTML, PDF = "text/html", "application/pdf"
CRAWL, LEAF, SKIP = "crawl", "leaf", "skip"
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


def user_agent_for(email: str) -> str:
    return USER_AGENT_TEMPLATE.format(email=email)


def request_url(href: str, base: str) -> str | None:
    """Absolute http(s) URL to request for a link, without its fragment; None otherwise.

    The path is kept as the server wrote it (trailing slash included), so a
    redirect from /uas to /uas/ does not loop back through canonical_url.
    """
    try:
        parts = urlsplit(urljoin(base, (href or "").strip()))
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host in (HOST, "faa.gov"):
        parts = parts._replace(scheme="https", netloc=HOST)
    return urlunsplit(parts._replace(fragment=""))


def normalize_link(href: str, base: str) -> str | None:
    """Canonical URL used as a link's identity (dedupe, manifest), or None."""
    url = request_url(href, base)
    if url is None:
        return None
    try:
        return canonical_url(url)
    except ValueError:
        return None


def scope(url: str) -> tuple[str, str]:
    """Decide what to do with a URL: (CRAWL | LEAF | SKIP, reason)."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != HOST:
        return SKIP, "other_host"
    path = parts.path.lower()
    for reason, pattern in EXCLUDED:
        if pattern.search(path):
            return SKIP, f"excluded_{reason}"
    suffix = PurePosixPath(path).suffix
    if suffix in SKIP_EXTENSIONS:
        return SKIP, "unsupported_file_type"
    if path == "/uas" or path.startswith(CRAWL_PREFIXES):
        return CRAWL, "drone_section"
    if suffix == ".pdf" or path.startswith(LEAF_PREFIXES):
        return LEAF, "linked_from_drone_section"
    return SKIP, "out_of_scope"


def content_kind(resp, url: str) -> str | None:
    """HTML or PDF from the Content-Type header (or PDF magic bytes); None for anything else."""
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype == PDF or resp.content[:5] == b"%PDF-":
        return PDF
    if ctype in (HTML, "application/xhtml+xml"):
        return HTML
    return None


def extract_links(body: bytes, base: str) -> list[str]:
    """Canonical URLs of every <a href> on an HTML page, in page order, deduped."""
    soup = BeautifulSoup(body, "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        link = normalize_link(a["href"], base)
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
    """Breadth-first crawl of the drone section with resume from the manifest.

    `session` only needs `.get(url, headers=, timeout=, allow_redirects=)`, so tests
    pass a fake site and never touch the network.
    """

    def __init__(
        self,
        session,
        user_agent: str,
        *,
        raw_dir: Path = paths.RAW,
        manifest_path: Path = paths.MANIFEST,
        skipped_path: Path = paths.SKIPPED,
        max_urls: int = MAX_URLS,
        max_depth: int = MAX_DEPTH,
        limiter: RateLimiter | None = None,
        sleep=time.sleep,
        retry_wait=None,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self.session = session
        self.user_agent = user_agent
        self.raw_dir = raw_dir
        self.manifest_path = manifest_path
        self.skipped_path = skipped_path
        self.max_urls = max_urls
        self.max_depth = max_depth
        self.timeout = timeout
        self.limiter = limiter or RateLimiter(MIN_INTERVAL_S, sleep=sleep)
        self._sleep = sleep
        self._retry_wait = (
            retry_wait if retry_wait is not None else wait_exponential(multiplier=2, min=2, max=60)
        )
        self.robots = RobotsChecker(user_agent, self._fetch_robots_txt)
        self.stats: Counter[str] = Counter()
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
        """
        current = url
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
            nxt = request_url(location, current)
            if nxt is None:
                return None, canonical_url(current), "bad_redirect"
            target = canonical_url(nxt)
            if in_scope_only:
                kind, reason = scope(target)
                if kind == SKIP:
                    return None, target, f"redirect_{reason}"
            elif urlsplit(target).hostname != HOST:
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

    def sitemap_seeds(self, url: str = SITEMAP_URL) -> list[str]:
        """Drone-section URLs listed in the sitemap (and its child sitemaps)."""
        pending, visited, seeds = [url], set(), []
        while pending and len(visited) < MAX_SITEMAPS:
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
                pending += [u for u in (request_url(x, sitemap) for x in locs) if u]
                continue
            for loc in locs:
                link = normalize_link(loc, sitemap)
                if link and scope(link)[0] == CRAWL and link not in seeds:
                    seeds.append(link)
        return seeds

    def run(self, seeds: list[str]) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        entries = read_manifest(self.manifest_path)
        done: dict[str, dict] = {}
        for e in entries:
            done[e["url"]] = e
            done.setdefault(e["final_url"], e)
        budget = self.max_urls - len(entries)

        queue: deque[tuple[str, int, str | None]] = deque()
        seen: set[str] = set()

        def enqueue(url: str, depth: int, found_on: str | None) -> None:
            if url in seen:
                return
            seen.add(url)
            kind, reason = scope(url)
            if kind == SKIP:
                self._skip(url, reason, depth, found_on)
            elif depth > self.max_depth:
                self._skip(url, "max_depth", depth, found_on)
            else:
                queue.append((url, depth, found_on))

        for seed in seeds:
            link = normalize_link(seed, START_URL)
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
                if budget <= 0:
                    self._skip(url, "max_urls", depth, found_on)
                    continue
                budget -= 1
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
                log.info("[%d] %s depth=%d %s", self.stats["saved"], kind, depth, url)

            if kind == HTML and body is not None and scope(final_url)[0] == CRAWL:
                for link in extract_links(body, final_url):
                    enqueue(link, depth + 1, url)


def crawl_stats(crawler: Crawler, started_at: str, n_seeds: int) -> dict:
    """Evidence for reports/01_ingestion.md, written to reports/artifacts/crawl_stats.json."""
    entries = read_manifest(crawler.manifest_path)
    gap = crawler.limiter.min_gap_observed
    return {
        "command": "make crawl",
        "started_at": started_at,
        "finished_at": _now(),
        # The real address is in .env; the artifact records only where it comes from.
        "user_agent": user_agent_for("<CRAWL_CONTACT_EMAIL>"),
        "limits": {
            "max_urls": crawler.max_urls,
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
        "resumed_from_manifest": crawler.stats["resumed"],
        "manifest": {
            "entries": len(entries),
            "unique_sha256": len({e["sha256"] for e in entries}),
            "by_content_type": dict(Counter(e["content_type"] for e in entries)),
        },
        "skipped_this_run_by_reason": dict(sorted(crawler.skipped.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crawl the FAA drone section once.")
    parser.add_argument("--force", action="store_true", help="run even if a full manifest exists")
    parser.add_argument("--max-urls", type=int, default=MAX_URLS)
    parser.add_argument("--max-depth", type=int, default=MAX_DEPTH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if should_refuse(paths.MANIFEST, args.force):
        print(
            f"refusing to crawl: {paths.MANIFEST} already has {REFUSE_AT}+ entries "
            "(crawl once; use FORCE=1 / --force only if the team agreed)"
        )
        return 1

    email = config.env("CRAWL_CONTACT_EMAIL", required=True)
    started_at = _now()
    with requests.Session() as session:
        crawler = Crawler(
            session, user_agent_for(email), max_urls=args.max_urls, max_depth=args.max_depth
        )
        seeds = [START_URL] + crawler.sitemap_seeds()
        log.info("%d seeds (start page + sitemap)", len(seeds))
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
