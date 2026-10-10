"""Crawler tests against a fake site: no network, no real sleeping (CLAUDE.md rule 1)."""

import json
from datetime import datetime
from pathlib import Path

import pytest
import requests
from tenacity import wait_none

from src.common.contracts import ManifestEntry, sha256_hex
from src.crawl import crawler as crawl_mod
from src.crawl.crawler import (
    CRAWL,
    LEAF,
    SKIP,
    Crawler,
    extract_links,
    normalize_link,
    read_manifest,
    scope,
    should_refuse,
    user_agent_for,
)
from src.crawl.ratelimit import RateLimiter
from src.crawl.robots import RobotsChecker

BASE = "https://www.faa.gov"
ROBOTS_URL = f"{BASE}/robots.txt"
UA = user_agent_for("team@example.com")


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class FakeResponse:
    def __init__(
        self, status=200, body=b"", content_type="text/html; charset=utf-8", location=None
    ):
        self.status_code = status
        self.content = body
        self.headers = {"Content-Type": content_type}
        if location:
            self.headers["Location"] = location


def page(*hrefs):
    links = "".join(f'<a href="{h}">link</a>' for h in hrefs)
    return f"<html><body>{links}</body></html>".encode()


class FakeSite:
    """Stands in for requests.Session. A list value is served in order (for retries)."""

    def __init__(self, pages, robots="User-agent: *\nAllow: /\n"):
        robots_resp = FakeResponse(body=robots.encode(), content_type="text/plain")
        self.pages = {ROBOTS_URL: robots_resp, **pages}
        self.requests = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        assert allow_redirects is False, "redirects must be followed by hand"
        self.requests.append((url, headers))
        resp = self.pages.get(url, FakeResponse(404, b"not found"))
        if isinstance(resp, list):
            return resp.pop(0) if len(resp) > 1 else resp[0]
        return resp

    def fetched(self):
        return [u for u, _ in self.requests if u != ROBOTS_URL]


def make_crawler(tmp_path, site, **kwargs):
    clock = FakeClock()
    raw = tmp_path / "raw"
    crawler = Crawler(
        site,
        UA,
        raw_dir=raw,
        manifest_path=raw / "manifest.jsonl",
        skipped_path=raw / "skipped.jsonl",
        limiter=RateLimiter(1.0, clock=clock.now, sleep=clock.sleep),
        sleep=clock.sleep,
        retry_wait=wait_none(),
        **kwargs,
    )
    return crawler, clock


def skipped(tmp_path):
    path = tmp_path / "raw" / "skipped.jsonl"
    return {r["url"]: r["reason"] for r in map(json.loads, path.read_text().splitlines())}


def manifest(tmp_path):
    return read_manifest(tmp_path / "raw" / "manifest.jsonl")


# --- Rate limit -------------------------------------------------------------


def test_rate_limiter_keeps_a_one_second_gap():
    clock = FakeClock()
    limiter = RateLimiter(1.0, clock=clock.now, sleep=clock.sleep)
    for _ in range(5):
        limiter.wait()
    assert clock.t == pytest.approx(4.0)
    assert limiter.min_gap_observed == pytest.approx(1.0)


def test_rate_limiter_does_not_sleep_when_enough_time_has_passed():
    clock = FakeClock()
    limiter = RateLimiter(1.0, clock=clock.now, sleep=clock.sleep)
    limiter.wait()
    clock.t += 2.5
    limiter.wait()
    assert clock.sleeps == []
    assert limiter.min_gap_observed == pytest.approx(2.5)


def test_crawler_requests_are_at_least_one_second_apart(tmp_path):
    site = FakeSite(
        {
            f"{BASE}/uas": FakeResponse(body=page("/uas/a", "/uas/b")),
            f"{BASE}/uas/a": FakeResponse(body=page()),
            f"{BASE}/uas/b": FakeResponse(body=page()),
        }
    )
    crawler, clock = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas"])
    assert len(site.requests) == 4  # robots.txt + 3 pages
    assert crawler.limiter.min_gap_observed >= 1.0
    assert clock.t >= 3.0


# --- robots.txt -------------------------------------------------------------


def test_robots_disallow_is_respected(tmp_path):
    site = FakeSite(
        {
            f"{BASE}/uas": FakeResponse(body=page("/uas/private/x", "/uas/ok")),
            f"{BASE}/uas/private/x": FakeResponse(body=page()),
            f"{BASE}/uas/ok": FakeResponse(body=page()),
        },
        robots="User-agent: *\nDisallow: /uas/private\n",
    )
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas"])
    assert f"{BASE}/uas/private/x" not in site.fetched()
    assert f"{BASE}/uas/ok" in site.fetched()
    assert skipped(tmp_path)[f"{BASE}/uas/private/x"] == "robots_disallowed"
    assert crawler.robots.denied == 1


def test_robots_txt_is_read_once_and_checked_before_every_fetch(tmp_path):
    site = FakeSite(
        {
            f"{BASE}/uas": FakeResponse(body=page("/uas/a", "/uas/b")),
            f"{BASE}/uas/a": FakeResponse(body=page()),
            f"{BASE}/uas/b": FakeResponse(body=page()),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas"])
    assert [u for u, _ in site.requests].count(ROBOTS_URL) == 1
    assert site.requests[0][0] == ROBOTS_URL
    assert crawler.robots.checks == 3


def test_crawl_delay_in_robots_txt_widens_the_gap(tmp_path):
    site = FakeSite(
        {f"{BASE}/uas": FakeResponse(body=page("/uas/a")), f"{BASE}/uas/a": FakeResponse()},
        robots="User-agent: *\nCrawl-delay: 3\n",
    )
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas"])
    assert crawler.limiter.min_interval == 3.0
    assert crawler.limiter.min_gap_observed >= 3.0


@pytest.mark.parametrize(
    "fetch, allowed",
    [
        (lambda url: (404, ""), True),  # no robots.txt means no rules
        (lambda url: (503, ""), False),  # unreadable: fail closed
        (lambda url: (403, ""), False),
    ],
)
def test_robots_status_handling(fetch, allowed):
    assert RobotsChecker(UA, fetch).allowed(f"{BASE}/uas") is allowed


def test_robots_network_error_fails_closed():
    def fetch(url):
        raise requests.ConnectionError("down")

    assert RobotsChecker(UA, fetch).allowed(f"{BASE}/uas") is False


# --- Scope and exclusions ---------------------------------------------------


@pytest.mark.parametrize(
    "url, kind, reason",
    [
        (f"{BASE}/uas", CRAWL, "drone_section"),
        (f"{BASE}/uas/recreational_flyers", CRAWL, "drone_section"),
        (f"{BASE}/sites/faa.gov/files/uas/resources/guide.pdf", CRAWL, "drone_section"),
        (
            f"{BASE}/documentLibrary/media/Advisory_Circular/AC_107-2A.pdf",
            LEAF,
            "linked_from_drone_section",
        ),
        (
            f"{BASE}/regulations_policies/advisory_circulars/index.cfm/go/document.information",
            LEAF,
            "linked_from_drone_section",
        ),
        (f"{BASE}/uas/contact_us", SKIP, "excluded_contact_page"),
        (f"{BASE}/uas/public_safety_gov/contacts", SKIP, "excluded_contact_page"),
        (f"{BASE}/uas/commercial_operators/waivers_issued", SKIP, "excluded_waivers_issued"),
        (f"{BASE}/uas/resources/public_records/uas_sightings_report", SKIP, "excluded_sightings"),
        (f"{BASE}/uas/espanol", SKIP, "excluded_spanish"),
        (f"{BASE}/uas/espanol/inicio", SKIP, "excluded_spanish"),
        (f"{BASE}/search?q=drone", SKIP, "excluded_search"),
        (f"{BASE}/user/login", SKIP, "excluded_user_account"),
        ("https://faadronezone-access.faa.gov/", SKIP, "other_host"),
        ("https://www.example.com/uas/", SKIP, "other_host"),
        (f"{BASE}/air_traffic/publications", SKIP, "out_of_scope"),
        (f"{BASE}/uas/images/drone.png", SKIP, "unsupported_file_type"),
    ],
)
def test_scope_rules(url, kind, reason):
    assert scope(url) == (kind, reason)


def test_normalize_link_makes_absolute_canonical_https_urls():
    base = f"{BASE}/uas/getting_started"
    assert normalize_link("../uas/recreational_flyers/#rules", base) == (
        f"{BASE}/uas/recreational_flyers"
    )
    assert normalize_link("http://faa.gov/uas/", base) == f"{BASE}/uas"
    assert normalize_link("mailto:someone@faa.gov", base) is None
    assert normalize_link("javascript:void(0)", base) is None


def test_extract_links_dedupes_in_page_order():
    body = page("/uas/b", "/uas/a", "/uas/b#x", "tel:123")
    assert extract_links(body, f"{BASE}/uas") == [f"{BASE}/uas/b", f"{BASE}/uas/a"]


def test_excluded_and_offsite_links_are_never_requested(tmp_path):
    site = FakeSite(
        {
            f"{BASE}/uas": FakeResponse(
                body=page(
                    "/uas/contact_us",
                    "https://faadronezone-access.faa.gov/",
                    "/uas/espanol",
                    "/air_traffic",
                )
            )
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas"])
    assert site.fetched() == [f"{BASE}/uas"]
    assert skipped(tmp_path) == {
        f"{BASE}/uas/contact_us": "excluded_contact_page",
        "https://faadronezone-access.faa.gov/": "other_host",
        f"{BASE}/uas/espanol": "excluded_spanish",
        f"{BASE}/air_traffic": "out_of_scope",
    }


def test_leaf_pages_are_fetched_but_not_expanded(tmp_path):
    ac = f"{BASE}/regulations_policies/advisory_circulars/ac-107-2"
    site = FakeSite(
        {
            f"{BASE}/uas": FakeResponse(body=page(ac)),
            ac: FakeResponse(body=page("/uas/only_linked_from_ac")),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas"])
    assert site.fetched() == [f"{BASE}/uas", ac]


# --- Raw store and manifest -------------------------------------------------


def test_raw_bytes_are_saved_before_the_manifest_line_and_match_the_contract(tmp_path):
    pdf = b"%PDF-1.7 fixture bytes"
    site = FakeSite(
        {
            f"{BASE}/uas": FakeResponse(body=page("/uas/guide.pdf")),
            f"{BASE}/uas/guide.pdf": FakeResponse(body=pdf, content_type="application/pdf"),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    original = crawler._append_manifest

    def append_checked(entry):
        assert Path(entry["path"]).exists(), "bytes must be on disk before the manifest line"
        original(entry)

    crawler._append_manifest = append_checked
    crawler.run([f"{BASE}/uas"])

    entries = manifest(tmp_path)
    assert len(entries) == 2
    for e in entries:
        assert set(e) == set(ManifestEntry.__annotations__)
        assert sha256_hex(Path(e["path"]).read_bytes()) == e["sha256"]
        assert e["status"] == 200 and e["robots_allowed"] is True
        assert datetime.fromisoformat(e["fetched_at"]).tzinfo is not None
    by_url = {e["url"]: e for e in entries}
    assert by_url[f"{BASE}/uas/guide.pdf"]["content_type"] == "application/pdf"
    assert by_url[f"{BASE}/uas"]["content_type"] == "text/html"


def test_unsupported_content_type_is_not_saved(tmp_path):
    site = FakeSite(
        {
            f"{BASE}/uas": FakeResponse(body=page("/uas/data")),
            f"{BASE}/uas/data": FakeResponse(body=b"{}", content_type="application/json"),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas"])
    assert [e["url"] for e in manifest(tmp_path)] == [f"{BASE}/uas"]
    assert skipped(tmp_path)[f"{BASE}/uas/data"] == "unsupported_content_type"


def test_user_agent_carries_the_contact_email(tmp_path):
    site = FakeSite({f"{BASE}/uas": FakeResponse(body=page())})
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas"])
    assert UA == "SkyRulesAI-MSAI633 (+team@example.com)"
    assert all(h == {"User-Agent": UA} for _, h in site.requests)


# --- Retries and redirects --------------------------------------------------


def test_retries_429_and_5xx_then_saves(tmp_path):
    site = FakeSite(
        {f"{BASE}/uas": [FakeResponse(503), FakeResponse(429), FakeResponse(body=page())]}
    )
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas"])
    assert site.fetched().count(f"{BASE}/uas") == 3
    assert len(manifest(tmp_path)) == 1


def test_gives_up_after_max_attempts(tmp_path):
    site = FakeSite({f"{BASE}/uas": [FakeResponse(503)]})
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas"])
    assert site.fetched().count(f"{BASE}/uas") == crawl_mod.MAX_ATTEMPTS
    assert manifest(tmp_path) == []
    assert skipped(tmp_path)[f"{BASE}/uas"] == "http_503_after_retries"


def test_redirect_to_another_host_is_not_followed(tmp_path):
    site = FakeSite(
        {
            f"{BASE}/uas": FakeResponse(body=page("/uas/dronezone")),
            f"{BASE}/uas/dronezone": FakeResponse(
                302, location="https://faadronezone-access.faa.gov/login"
            ),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas"])
    assert not any("faadronezone" in u for u in site.fetched())
    assert skipped(tmp_path)[f"{BASE}/uas/dronezone"] == "redirect_other_host"


def test_trailing_slash_redirect_does_not_loop(tmp_path):
    site = FakeSite(
        {
            f"{BASE}/uas": FakeResponse(301, location="/uas/"),
            f"{BASE}/uas/": FakeResponse(body=page()),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas"])
    assert site.fetched() == [f"{BASE}/uas", f"{BASE}/uas/"]
    assert len(manifest(tmp_path)) == 1


def test_in_scope_redirect_records_final_url(tmp_path):
    site = FakeSite(
        {
            f"{BASE}/uas/old": FakeResponse(301, location="/uas/new"),
            f"{BASE}/uas/new": FakeResponse(body=page()),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas/old"])
    [entry] = manifest(tmp_path)
    assert entry["url"] == f"{BASE}/uas/old"
    assert entry["final_url"] == f"{BASE}/uas/new"


# --- Limits, resume and the crawl-once guard --------------------------------


def test_max_depth_stops_the_crawl(tmp_path):
    chain = ["/uas", "/uas/d1", "/uas/d2", "/uas/d3", "/uas/d4"]
    links = chain[1:] + [""]
    site = FakeSite(
        {f"{BASE}{p}": FakeResponse(body=page(n)) for p, n in zip(chain, links, strict=True)}
    )
    crawler, _ = make_crawler(tmp_path, site, max_depth=3)
    crawler.run([f"{BASE}/uas"])
    assert site.fetched() == [f"{BASE}{p}" for p in chain[:4]]
    assert skipped(tmp_path)[f"{BASE}/uas/d4"] == "max_depth"


def test_max_urls_caps_the_crawl(tmp_path):
    site = FakeSite(
        {
            f"{BASE}/uas": FakeResponse(body=page("/uas/a", "/uas/b", "/uas/c")),
            **{f"{BASE}/uas/{x}": FakeResponse(body=page()) for x in "abc"},
        }
    )
    crawler, _ = make_crawler(tmp_path, site, max_urls=2)
    crawler.run([f"{BASE}/uas"])
    assert len(site.fetched()) == 2
    assert skipped(tmp_path)[f"{BASE}/uas/b"] == "max_urls"


def test_resume_skips_fetched_urls_but_follows_their_saved_links(tmp_path):
    pages = {
        f"{BASE}/uas": FakeResponse(body=page("/uas/a")),
        f"{BASE}/uas/a": FakeResponse(body=page()),
    }
    first, _ = make_crawler(tmp_path, FakeSite(dict(pages)), max_urls=1)
    first.run([f"{BASE}/uas"])
    assert [e["url"] for e in manifest(tmp_path)] == [f"{BASE}/uas"]

    site = FakeSite(dict(pages))
    second, _ = make_crawler(tmp_path, site)
    second.run([f"{BASE}/uas"])
    assert site.fetched() == [f"{BASE}/uas/a"]  # /uas came from the saved bytes
    assert second.stats["resumed"] == 1
    assert [e["url"] for e in manifest(tmp_path)] == [f"{BASE}/uas", f"{BASE}/uas/a"]


def test_refuses_a_second_full_crawl_without_force(tmp_path):
    path = tmp_path / "manifest.jsonl"
    assert not should_refuse(path, force=False)  # no manifest yet
    path.write_text("".join(json.dumps({"url": f"u{i}"}) + "\n" for i in range(150)))
    assert should_refuse(path, force=False)
    assert not should_refuse(path, force=True)


def test_sitemap_seeds_keep_only_drone_pages(tmp_path):
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
    index = (
        f"<sitemapindex {ns}><sitemap><loc>{BASE}/sitemap.xml?page=2</loc></sitemap></sitemapindex>"
    )
    urls = ["/uas/getting_started", "/air_traffic", "/uas/contact_us", "/uas/getting_started/"]
    urlset = f"<urlset {ns}>" + "".join(f"<url><loc>{BASE}{u}</loc></url>" for u in urls)
    urlset += "</urlset>"
    site = FakeSite(
        {
            f"{BASE}/sitemap.xml": FakeResponse(
                body=index.encode(), content_type="application/xml"
            ),
            f"{BASE}/sitemap.xml?page=2": FakeResponse(
                body=urlset.encode(), content_type="application/xml"
            ),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    assert crawler.sitemap_seeds() == [f"{BASE}/uas/getting_started"]
