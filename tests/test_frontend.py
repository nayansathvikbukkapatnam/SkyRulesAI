"""Frontend checks: the page and its files are served, keep the spec's labels and wording,
load nothing unexpected, and the mock events match what the server really sends."""

import json
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from src.app import mock_events
from src.app.server import STATIC_DIR, create_app
from src.common import paths
from src.common.config import load_config

INDEX = (STATIC_DIR / "index.html").read_text()
APP_JS = (STATIC_DIR / "app.js").read_text()
STYLES = (STATIC_DIR / "styles.css").read_text()
MOCK = json.loads((STATIC_DIR / "mock_events.json").read_text())
ANSWER_KEYS = {
    "status",
    "sentences",
    "citations",
    "referral",
    "router_tag",
    "rewritten_question",
    "trace",
    "latency_ms",
    "cost_usd",
    "config",
}


@pytest.fixture
def client():
    app = create_app(
        answer_fn=lambda *a, **k: {},
        configs={"b2": load_config("b2"), "b3": load_config("b3")},
        client=object(),
        retrieve_fn=lambda *a, **k: [],
        rerank_fn=lambda *a, **k: [],
    )
    with TestClient(app) as tc:
        yield tc


# --- Served files ------------------------------------------------------------------------


def test_root_serves_the_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert '<script src="/static/app.js"></script>' in r.text
    assert '<link rel="stylesheet" href="/static/styles.css">' in r.text


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/static/app.js", "javascript"),
        ("/static/styles.css", "text/css"),
        ("/static/mock_events.json", "json"),
    ],
)
def test_static_files_are_served(client, path, kind):
    r = client.get(path)
    assert r.status_code == 200 and kind in r.headers["content-type"]


# --- Labels, accessibility and wording (UI_SPEC sections 4 and 6) ----------------------------


def test_page_has_the_required_controls_and_labels():
    assert '<label for="ask-input" class="sr-only">Your drone question</label>' in INDEX
    assert 'placeholder="Ask about registration, airspace, Remote ID, night flying..."' in INDEX
    assert INDEX.count('class="pilot-btn"') == 3
    assert 'data-pilot="not_sure" aria-pressed="true"' in INDEX
    assert 'id="compare-btn" class="head-btn compare" aria-pressed="false"' in INDEX
    assert 'id="theme-btn" class="head-btn" aria-pressed="false"' in INDEX
    assert 'id="trace-toggle" class="trace-toggle" aria-expanded="false"' in INDEX
    assert 'aria-live="polite"' in INDEX
    for chip in (
        "How high can I fly as a hobby flyer?",
        "Can I fly in San Diego tonight at 10 pm?",
        "Which drone should I buy?",
    ):
        assert chip in INDEX


def test_reliability_note_and_refusal_wording():
    assert "Student project, not official FAA guidance." in INDEX
    assert "I don't have that information in the official sources I can search." in APP_JS
    assert "Something went wrong, so SkyRules did not answer. Try again." in APP_JS
    assert "Know Before You Fly" in INDEX


@pytest.mark.parametrize("name", ["index.html", "app.js", "styles.css"])
def test_no_outdated_faa_pages_wording(name):
    text = (STATIC_DIR / name).read_text()
    assert not re.search(r"FAA pages|FAA page\b|official FAA drone pages|FAA passages", text)


def test_ui_spec_matches_the_new_sources():
    spec = (paths.ROOT / "docs" / "UI_SPEC.md").read_text()
    assert not re.search(r"FAA pages|FAA page\b|official FAA drone pages|FAA passages", spec)
    assert "I don't have that information in the official sources I can search." in spec
    assert "—" not in spec and "–" not in spec


def test_both_themes_define_every_token():
    light = set(re.findall(r"--([a-zA-Z0-9]+):", STYLES.split(':root[data-theme="dark"]')[0]))
    dark_block = STYLES.split(':root[data-theme="dark"]')[1].split("}")[0]
    dark = set(re.findall(r"--([a-zA-Z0-9]+):", dark_block))
    theme_tokens = light - {"orange", "progress", "check", "sans", "mono"}
    assert theme_tokens <= dark
    assert "prefers-reduced-motion" in STYLES


# --- What the page loads and how it treats server text --------------------------------------


def test_only_google_fonts_are_loaded_from_outside():
    hosts = set(re.findall(r'(?:href|src)="https?://([^/"]+)', INDEX))
    assert hosts <= {"fonts.googleapis.com", "fonts.gstatic.com"}


def test_app_links_only_to_the_faa_referral_tools():
    urls = set(re.findall(r'"(https?://[^"]+)"', APP_JS))
    assert urls == {
        "https://www.faa.gov/uas/getting_started/b4ufly",
        "https://www.faa.gov/uas/commercial_operators/uas_facility_maps",
        "https://www.faa.gov/uas",
    }


def test_server_text_is_never_inserted_as_html():
    # innerHTML appears once, in icon(), which only ever receives the constant ICONS markup.
    assert APP_JS.count("innerHTML") == 1
    assert re.search(
        r"function icon\(markup\) \{\s+const t = document.createElement\(\"template\"\);"
        r"\s+t.innerHTML",
        APP_JS,
    )
    assert "icon(ICONS." in APP_JS and not re.search(
        r"icon\((?!ICONS\.)[a-z]", APP_JS.split("function icon")[1]
    )
    for risky in ("eval(", "document.write", "insertAdjacentHTML", "outerHTML"):
        assert risky not in APP_JS


def test_links_from_answers_are_checked():
    assert "function safeUrl(url)" in APP_JS
    assert 'rel: "noopener noreferrer"' in APP_JS


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_app_js_parses():
    subprocess.run(["node", "--check", str(STATIC_DIR / "app.js")], check=True)


# --- Mock events match the real event stream ------------------------------------------------


@pytest.mark.parametrize("scenario", ["answer", "location", "refusal"])
def test_mock_events_have_the_server_shape(scenario):
    events = MOCK["scenarios"][scenario]
    assert events[-1] == {"event": "done", "data": {}}
    stages = [e["data"] for e in events if e["event"] == "stage"]
    assert stages and all(set(s) == {"name", "status", "ms", "config"} for s in stages)
    assert {s["status"] for s in stages} <= {"started", "done", "stopped", "skipped"}
    answers = [e["data"] for e in events if e["event"] == "answer"]
    assert [a["config"] for a in answers] == ["b3", "b2"]
    for a in answers:
        assert ANSWER_KEYS <= set(a)
    assert "b3_check_flags" in answers[1]
    titles = {c["title"] for a in answers for c in a["trace"]["candidates"]}
    assert titles and all(t.startswith("[FIXTURE]") for t in titles)  # never real corpus text


def test_mock_scenarios_cover_the_demo():
    by = {
        k: [e["data"] for e in v if e["event"] == "answer"][0] for k, v in MOCK["scenarios"].items()
    }
    assert by["answer"]["status"] == "answered" and by["answer"]["citations"]
    assert by["location"]["referral"]["kind"] == "location"
    assert by["refusal"]["status"] == "refused"


def test_mock_file_is_up_to_date(monkeypatch, tmp_path):
    try:  # once the query log is merged, keep this run out of logs/
        from src.generate import log as log_mod

        monkeypatch.setattr(log_mod, "QUERY_LOG", tmp_path / "q.jsonl")
    except ImportError:
        pass
    assert mock_events.build() == MOCK, "run: python -m src.app.mock_events"
