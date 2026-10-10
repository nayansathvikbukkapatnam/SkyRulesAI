"""Crawler tests against fake sites: no network, no real sleeping (CLAUDE.md rule 1)."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
import requests
from tenacity import wait_none

from src.common.contracts import ManifestEntry, sha256_hex
from src.crawl import crawler as crawl_mod
from src.crawl.crawler import (
    Crawler,
    extract_links,
    read_manifest,
    should_refuse,
    user_agent_for,
)
from src.crawl.ratelimit import RateLimiter
from src.crawl.robots import RobotsChecker
from src.crawl.sources import CRAWL, LEAF, SKIP, SourceSet

BASE = "https://www.faa.gov"
ECFR = "https://www.ecfr.gov"
FR = "https://www.federalregister.gov"
GOVINFO = "https://www.govinfo.gov"
KBYF = "https://knowbeforeyoufly.org"
ECFR_SECTION = f"{ECFR}/api/renderer/v1/content/enhanced/current/title-14?section="
UA = user_agent_for("team@example.com")
SOURCES = SourceSet.load()  # the real configs/crawl/sources.yaml


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
    """Stands in for requests.Session across hosts. A list value is served in order.

    Any /robots.txt not given explicitly allows everything.
    """

    def __init__(self, pages, robots="User-agent: *\nAllow: /\n"):
        self.pages = pages
        self.robots = robots
        self.requests = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        assert allow_redirects is False, "redirects must be followed by hand"
        self.requests.append((url, headers))
        if url.endswith("/robots.txt") and url not in self.pages:
            return FakeResponse(body=self.robots.encode(), content_type="text/plain")
        resp = self.pages.get(url, FakeResponse(404, b"not found"))
        if isinstance(resp, list):
            return resp.pop(0) if len(resp) > 1 else resp[0]
        return resp

    def fetched(self):
        return [u for u, _ in self.requests if not u.endswith("/robots.txt")]

    def robots_requests(self):
        return [u for u, _ in self.requests if u.endswith("/robots.txt")]


def make_crawler(tmp_path, site, sources=SOURCES, **kwargs):
    clock = FakeClock()
    raw = tmp_path / "raw"
    crawler = Crawler(
        site,
        UA,
        sources,
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


def test_crawler_requests_are_at_least_one_second_apart_across_sites(tmp_path):
    site = FakeSite(
        {
            f"{BASE}/uas": FakeResponse(body=page("/uas/a")),
            f"{BASE}/uas/a": FakeResponse(body=page()),
            f"{KBYF}/": FakeResponse(body=page("/about")),
            f"{KBYF}/about": FakeResponse(body=page()),
        }
    )
    crawler, clock = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas", f"{KBYF}/"])
    assert len(site.requests) == 6  # 2 robots.txt + 4 pages
    assert crawler.limiter.min_gap_observed >= 1.0
    assert clock.t >= 5.0


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


def test_robots_txt_is_read_once_per_site_and_checked_before_every_fetch(tmp_path):
    site = FakeSite(
        {
            f"{BASE}/uas": FakeResponse(body=page("/uas/a", "/uas/b")),
            f"{BASE}/uas/a": FakeResponse(body=page()),
            f"{BASE}/uas/b": FakeResponse(body=page()),
            f"{KBYF}/": FakeResponse(body=page()),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas", f"{KBYF}/"])
    assert sorted(site.robots_requests()) == [f"{KBYF}/robots.txt", f"{BASE}/robots.txt"]
    assert crawler.robots.checks == 4


def test_a_blocked_site_does_not_stop_the_others(tmp_path):
    site = FakeSite(
        {
            f"{BASE}/robots.txt": FakeResponse(403, b"Access Denied"),
            f"{BASE}/uas": FakeResponse(body=page()),
            f"{KBYF}/": FakeResponse(body=page()),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([f"{BASE}/uas", f"{KBYF}/"])
    assert site.fetched() == [f"{KBYF}/"]
    assert skipped(tmp_path)[f"{BASE}/uas"] == "robots_disallowed"


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


WILDCARD_ROBOTS = """\
User-agent: *
Disallow: /*.json$
Disallow: /*?s=
Disallow: /private/
Allow: /private/public-page
Allow: /core/*.css$
Disallow: /core/
"""


@pytest.mark.parametrize(
    "path, allowed",
    [
        ("/data/rules.json", False),  # "*" and "$" are honoured
        ("/data/rules.json?x=1", True),  # "$" anchors the end of the URL
        ("/page?s=drone", False),
        ("/private/secret", False),
        ("/private/public-page", True),  # the longer Allow beats the shorter Disallow
        ("/core/theme.css", True),
        ("/core/script.js", False),
        ("/uas/getting_started", True),
    ],
)
def test_robots_wildcards_and_longest_match(path, allowed):
    checker = RobotsChecker(UA, lambda url: (200, WILDCARD_ROBOTS))
    assert checker.allowed(f"{KBYF}{path}") is allowed


def test_robots_group_for_our_user_agent_wins_over_star():
    robots = "User-agent: *\nDisallow: /\n\nUser-agent: SkyRulesAI-MSAI633\nAllow: /\n"
    checker = RobotsChecker(UA, lambda url: (200, robots))
    assert checker.allowed(f"{KBYF}/anything") is True


# --- Scope and exclusions ---------------------------------------------------


@pytest.mark.parametrize(
    "url, kind, reason, source",
    [
        # FAA (rule 2 exclusions kept from the original crawler)
        (f"{BASE}/uas", CRAWL, "in_scope", "faa"),
        (f"{BASE}/uas/recreational_flyers", CRAWL, "in_scope", "faa"),
        (f"{BASE}/sites/faa.gov/files/uas/resources/guide.pdf", CRAWL, "in_scope", "faa"),
        (f"{BASE}/documentLibrary/media/Advisory_Circular/AC_107-2A.pdf", LEAF, None, "faa"),
        (f"{BASE}/regulations_policies/advisory_circulars/index.cfm", LEAF, None, "faa"),
        (f"{BASE}/uas/contact_us", SKIP, "excluded_contact_page", "faa"),
        (f"{BASE}/uas/public_safety_gov/contacts", SKIP, "excluded_contact_page", "faa"),
        (f"{BASE}/uas/part_107_waivers/waivers_issued", SKIP, "excluded_waivers_issued", "faa"),
        (f"{BASE}/uas/resources/uas_sightings_report", SKIP, "excluded_sightings", "faa"),
        (f"{BASE}/uas/espanol/inicio", SKIP, "excluded_spanish", "faa"),
        (f"{BASE}/search?q=drone", SKIP, "excluded_search", "faa"),
        (f"{BASE}/user/login", SKIP, "excluded_login", "faa"),
        (f"{BASE}/air_traffic/publications", SKIP, "out_of_scope", "faa"),
        (f"{BASE}/uas/images/drone.png", SKIP, "unsupported_file_type", "faa"),
        # eCFR: only the renderer API (the web pages are closed to automated clients)
        (f"{ECFR_SECTION}107.31", LEAF, "linked_document", "ecfr"),
        (f"{ECFR}/current/title-14/section-107.31", SKIP, "out_of_scope", "ecfr"),
        (f"{ECFR}/api/versioner/v1/titles", SKIP, "out_of_scope", "ecfr"),
        # Federal Register: only the full text of the listed drone rules
        (f"{FR}/documents/full_text/html/2021/01/15/2020-28948.html", LEAF, None, None),
        (f"{FR}/documents/full_text/html/2021/01/15/2021-00001.html", SKIP, "out_of_scope", None),
        (f"{FR}/d/2020-28948", SKIP, "out_of_scope", "federalregister"),
        # govinfo: only Federal Register PDFs, as leaves
        (f"{GOVINFO}/content/pkg/FR-2021-01-15/pdf/2020-28948.pdf", LEAF, None, "govinfo"),
        (f"{GOVINFO}/app/collection/fr", SKIP, "out_of_scope", "govinfo"),
        # Know Before You Fly: guidance sections only, not the news posts
        (f"{KBYF}/", CRAWL, "in_scope", "knowbeforeyoufly"),
        (f"{KBYF}/whos-flying/recreational/", CRAWL, "in_scope", "knowbeforeyoufly"),
        (f"{KBYF}/get-started/register-your-drone/", CRAWL, "in_scope", "knowbeforeyoufly"),
        (f"{KBYF}/good-drone-news-in-the-empire-state", SKIP, "out_of_scope", None),
        (f"{KBYF}/wp-content/uploads/2024/01/guide.pdf", LEAF, None, "knowbeforeyoufly"),
        (f"{KBYF}/contact-us/", SKIP, "excluded_contact_page", "knowbeforeyoufly"),
        (f"{KBYF}/wp-admin/", SKIP, "excluded_site_admin", "knowbeforeyoufly"),
        (f"{KBYF}/wp-login.php", SKIP, "excluded_login", "knowbeforeyoufly"),
        (f"{KBYF}/sign-in?redirect_to=x", SKIP, "excluded_login", "knowbeforeyoufly"),
        (f"{KBYF}/?s=drone", SKIP, "excluded_search", "knowbeforeyoufly"),
        (f"{KBYF}/tag/safety/", SKIP, "excluded_archive_listing", "knowbeforeyoufly"),
        (f"{KBYF}/feed/", SKIP, "excluded_feed", "knowbeforeyoufly"),
        # Never listed, never requested
        ("https://faadronezone-access.faa.gov/", SKIP, "other_host", None),
        ("https://web.archive.org/web/2026/https://www.faa.gov/uas", SKIP, "other_host", None),
    ],
)
def test_scope_rules(url, kind, reason, source):
    got_kind, got_reason, got_source = SOURCES.scope(url)
    assert got_kind == kind
    if reason:
        assert got_reason == reason
    if source:
        assert got_source == source


def test_disabled_sources_are_not_loaded():
    assert "wayback" not in {s.name for s in SOURCES.sources}
    with pytest.raises(ValueError):
        SourceSet.load(only=["wayback"])


def test_normalize_maps_aliases_to_the_main_host_over_https():
    base = f"{BASE}/uas/getting_started"
    assert SOURCES.normalize("../uas/recreational_flyers/#rules", base) == (
        f"{BASE}/uas/recreational_flyers"
    )
    assert SOURCES.normalize("http://faa.gov/uas/", base) == f"{BASE}/uas"
    assert SOURCES.normalize("http://www.knowbeforeyoufly.org/x", base) == f"{KBYF}/x"
    assert SOURCES.normalize("mailto:someone@faa.gov", base) is None
    assert SOURCES.normalize("javascript:void(0)", base) is None


def test_extract_links_dedupes_in_page_order():
    body = page("/uas/b", "/uas/a", "/uas/b#x", "tel:123")
    assert extract_links(body, f"{BASE}/uas", SOURCES) == [f"{BASE}/uas/b", f"{BASE}/uas/a"]


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


def test_federal_register_full_text_is_saved_but_its_links_are_not_followed(tmp_path):
    doc = f"{FR}/documents/full_text/html/2021/01/15/2020-28948.html"
    pdf = f"{GOVINFO}/content/pkg/FR-2021-01-15/pdf/2020-28948.pdf"
    site = FakeSite(
        {
            doc: FakeResponse(body=page(f"{FR}/documents/2021/01/15/2021-00001/unrelated")),
            pdf: FakeResponse(body=b"%PDF-1.7 rule", content_type="application/pdf"),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    crawler.run([doc, pdf])
    assert site.fetched() == [doc, pdf]
    assert crawler.saved_by_source == {"federalregister": 1, "govinfo": 1}
    assert {e["content_type"] for e in manifest(tmp_path)} == {"text/html", "application/pdf"}


def test_ecfr_index_lists_sections_which_are_saved_one_document_each(tmp_path):
    part = f"{ECFR}/api/renderer/v1/content/enhanced/current/title-14?part=107"
    full = f"{part}&chapter=I&subchapter=F"
    index_html = (
        '<div class="part"><div class="section" id="107.1">a</div>'
        '<div class="section" id="107.31">b</div><div class="section" id="107.1">dup</div>'
        '<div class="section" id="101.1">balloons</div>'
        "</div>"
    )
    sources = SourceSet.load(only=["ecfr"])
    sources.sources[0] = replace(sources.sources[0], index_urls=(part,))
    site = FakeSite(
        {
            part: FakeResponse(302, location=full),
            full: FakeResponse(body=index_html.encode()),
            f"{ECFR_SECTION}107.1": FakeResponse(body=b"<h4>107.1 Applicability</h4>"),
            f"{ECFR_SECTION}107.31": FakeResponse(body=b"<h4>107.31 Visual line of sight</h4>"),
        }
    )
    crawler, _ = make_crawler(tmp_path, site, sources=sources)
    seeds = crawler.index_seeds()
    assert seeds == [f"{ECFR_SECTION}{n}" for n in ("107.1", "107.31")]  # not 101.1
    crawler.run(seeds)
    assert [e["url"] for e in manifest(tmp_path)] == seeds  # the index page itself is not saved


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


def test_max_urls_caps_the_whole_crawl(tmp_path):
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


def test_per_source_cap_leaves_room_for_other_sources(tmp_path):
    site = FakeSite(
        {
            f"{BASE}/uas": FakeResponse(body=page("/uas/a", "/uas/b")),
            **{f"{BASE}/uas/{x}": FakeResponse(body=page()) for x in "ab"},
            f"{KBYF}/": FakeResponse(body=page("/get-started")),
            f"{KBYF}/get-started": FakeResponse(body=page()),
        }
    )
    crawler, _ = make_crawler(tmp_path, site, sources=SourceSet.load(cap=2))
    crawler.run([f"{BASE}/uas", f"{KBYF}/"])
    assert crawler.saved_by_source == {"faa": 2, "knowbeforeyoufly": 2}
    assert skipped(tmp_path)[f"{BASE}/uas/b"] == "max_urls_for_source"


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


def test_sitemap_seeds_keep_only_in_scope_pages_of_that_source(tmp_path):
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
    index = (
        f"<sitemapindex {ns}><sitemap><loc>{KBYF}/page-sitemap.xml</loc></sitemap>"
        f"<sitemap><loc>https://evil.example.com/sitemap.xml</loc></sitemap></sitemapindex>"
    )
    urls = [f"{KBYF}/get-started/", f"{KBYF}/contact-us/", f"{BASE}/uas", f"{KBYF}/a-news-post"]
    urlset = f"<urlset {ns}>" + "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    urlset += "</urlset>"
    site = FakeSite(
        {
            f"{KBYF}/sitemap_index.xml": FakeResponse(
                body=index.encode(), content_type="application/xml"
            ),
            f"{KBYF}/page-sitemap.xml": FakeResponse(
                body=urlset.encode(), content_type="application/xml"
            ),
        }
    )
    crawler, _ = make_crawler(tmp_path, site, sources=SourceSet.load(only=["knowbeforeyoufly"]))
    assert crawler.sitemap_seeds() == [f"{KBYF}/get-started"]
    assert not any("evil.example.com" in u for u in site.fetched())
