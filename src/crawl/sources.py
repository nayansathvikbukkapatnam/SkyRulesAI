"""Which sites and paths the crawler may fetch, loaded from configs/crawl/sources.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit, urlunsplit

import yaml

from src.common import paths
from src.common.contracts import canonical_url

SOURCES_FILE = paths.CONFIGS / "crawl" / "sources.yaml"

CRAWL, LEAF, SKIP = "crawl", "leaf", "skip"
SKIP_EXTENSIONS = frozenset(
    ".jpg .jpeg .png .gif .svg .webp .ico .bmp .tif .tiff .mp4 .mov .avi .mp3 .wav "
    ".zip .gz .css .js .xml .json .rss .doc .docx .xls .xlsx .ppt .pptx .kml .kmz".split()
)


@dataclass(frozen=True)
class Index:
    urls: tuple[str, ...]
    pattern: re.Pattern
    template: str


@dataclass(frozen=True)
class Source:
    name: str
    host: str
    aliases: tuple[str, ...]
    seeds: tuple[str, ...]
    sitemap: str | None
    max_urls: int
    crawl: tuple[re.Pattern, ...]
    leaf: tuple[re.Pattern, ...]
    exclude: tuple[tuple[str, re.Pattern], ...]
    # Discovery pages (read like a sitemap, never saved). Each index is
    # (urls, pattern, template): every match of pattern in a url's text becomes a
    # seed via template.format(*groups).
    indexes: tuple[Index, ...] = ()


def _patterns(items) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p) for p in items or [])


def _named(mapping) -> tuple[tuple[str, re.Pattern], ...]:
    return tuple((name, re.compile(p)) for name, p in (mapping or {}).items())


def _indexes(raw) -> tuple[Index, ...]:
    """`index:` may be one mapping or a list of them (e.g. one per CFR volume)."""
    items = raw if isinstance(raw, list) else [raw] if raw else []
    out = []
    for item in items:
        if not item.get("pattern") or not item.get("template"):
            raise ValueError("an index needs both a pattern and a template")
        out.append(
            Index(tuple(item.get("urls") or []), re.compile(item["pattern"]), item["template"])
        )
    return tuple(out)


class SourceSet:
    """The allowed sites, and the scope decision for any URL."""

    def __init__(self, sources: list[Source], exclude_all, max_urls_total: int) -> None:
        self.sources = sources
        self.exclude_all = exclude_all
        self.max_urls_total = max_urls_total
        self._by_host = {h: s for s in sources for h in (s.host, *s.aliases)}

    @classmethod
    def load(
        cls,
        path: Path = SOURCES_FILE,
        only: list[str] | None = None,
        cap: int | None = None,
        include_disabled: bool = False,
    ) -> SourceSet:
        """Enabled sources from the YAML. `only` picks sources by name; `cap` limits
        every source to that many URLs (for a small dry run); `include_disabled` is
        for tests of a disabled source's rules."""
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        sources = []
        for s in raw["sources"]:
            disabled = not s.get("enabled", True) and not include_disabled
            if disabled or (only and s["name"] not in only):
                continue
            source = Source(
                name=s["name"],
                host=s["host"].lower(),
                aliases=tuple(a.lower() for a in s.get("aliases") or []),
                seeds=tuple(s.get("seeds") or []),
                sitemap=s.get("sitemap"),
                max_urls=int(s["max_urls"]),
                crawl=_patterns(s.get("crawl")),
                leaf=_patterns(s.get("leaf")),
                exclude=_named(s.get("exclude")),
                indexes=_indexes(s.get("index")),
            )
            if cap is not None:
                source = replace(source, max_urls=min(source.max_urls, cap))
            sources.append(source)
        if only and {s.name for s in sources} != set(only):
            unknown = set(only) - {s.name for s in sources}
            raise ValueError(f"unknown or disabled sources: {sorted(unknown)}")
        return cls(sources, _named(raw.get("exclude_all")), int(raw["max_urls_total"]))

    def source_for(self, url: str) -> Source | None:
        return self._by_host.get((urlsplit(url).hostname or "").lower())

    def request_url(self, href: str, base: str) -> str | None:
        """Absolute http(s) URL to request for a link, without its fragment; None otherwise.

        Known hosts are mapped to their main host over https. The path is kept as
        the server wrote it, so a redirect from /uas to /uas/ does not loop.
        """
        try:
            parts = urlsplit(urljoin(base, (href or "").strip()))
        except ValueError:
            return None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        source = self._by_host.get(parts.hostname.lower())
        if source is not None:
            parts = parts._replace(scheme="https", netloc=source.host)
        return urlunsplit(parts._replace(fragment=""))

    def normalize(self, href: str, base: str) -> str | None:
        """Canonical URL used as a link's identity (dedupe, manifest), or None."""
        url = self.request_url(href, base)
        if url is None:
            return None
        try:
            return canonical_url(url)
        except ValueError:
            return None

    def scope(self, url: str) -> tuple[str, str, str | None]:
        """(CRAWL | LEAF | SKIP, reason, source name) for a URL."""
        source = self.source_for(url)
        if source is None:
            return SKIP, "other_host", None
        parts = urlsplit(url)
        path = parts.path.lower() or "/"
        target = f"{path}?{parts.query.lower()}" if parts.query else path
        for reason, pattern in (*self.exclude_all, *source.exclude):
            if pattern.search(target):
                return SKIP, f"excluded_{reason}", source.name
        is_leaf = any(p.search(path) for p in source.leaf)
        if PurePosixPath(path).suffix in SKIP_EXTENSIONS and not is_leaf:
            return SKIP, "unsupported_file_type", source.name
        if any(p.search(path) for p in source.crawl):
            return CRAWL, "in_scope", source.name
        if is_leaf:
            return LEAF, "linked_document", source.name
        return SKIP, "out_of_scope", source.name
