"""Plain data containers shared by the crawler, importers and analysis code."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: ``rel`` values that tell search engines not to pass ranking signals.
NOFOLLOW_RELS = frozenset({"nofollow", "ugc", "sponsored"})

#: Recognised link positions, in display order.
POSITIONS = ("main", "nav", "header", "footer", "aside", "body")

FORMAT_VERSION = 1


@dataclass
class Link:
    """A single ``<a href>`` occurrence found on ``source`` pointing at ``target``."""

    source: str
    target: str
    anchor: str = ""
    rel: tuple[str, ...] = ()
    position: str = "body"
    is_image: bool = False
    internal: bool = True
    nofollow: bool = False

    @property
    def follow(self) -> bool:
        return not self.nofollow

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"source": self.source, "target": self.target}
        if self.anchor:
            data["anchor"] = self.anchor
        if self.rel:
            data["rel"] = list(self.rel)
        if self.position != "body":
            data["position"] = self.position
        if self.is_image:
            data["is_image"] = True
        if not self.internal:
            data["internal"] = False
        if self.nofollow:
            data["nofollow"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Link:
        rel = data.get("rel") or ()
        if isinstance(rel, str):
            rel = rel.split()
        rel = tuple(str(value).lower() for value in rel)
        nofollow = bool(data.get("nofollow", False)) or any(r in NOFOLLOW_RELS for r in rel)
        return cls(
            source=data["source"],
            target=data["target"],
            anchor=data.get("anchor", "") or "",
            rel=rel,
            position=data.get("position", "body") or "body",
            is_image=bool(data.get("is_image", False)),
            internal=bool(data.get("internal", True)),
            nofollow=nofollow,
        )


@dataclass
class Page:
    """Everything known about one URL.

    ``status`` is ``0`` when the URL was never fetched (for example a link
    target beyond the crawl limits or a node from an imported edge list).
    """

    url: str
    status: int = 0
    title: str = ""
    h1: str = ""
    word_count: int = 0
    canonical: str = ""
    meta_robots: str = ""
    x_robots: str = ""
    content_type: str = ""
    depth: int | None = None
    redirect_target: str = ""
    redirect_chain: list[str] = field(default_factory=list)
    in_sitemap: bool = False
    crawled: bool = False
    blocked_by_robots: bool = False
    error: str = ""
    text: str = ""

    @property
    def is_redirect(self) -> bool:
        return self.status in REDIRECT_STATUSES or bool(self.redirect_target)

    @property
    def is_broken(self) -> bool:
        """4xx/5xx response or a fetch error on a URL we attempted to crawl."""
        if self.status >= 400:
            return True
        return self.crawled and self.status == 0 and bool(self.error) and not self.blocked_by_robots

    @property
    def is_ok(self) -> bool:
        """A normal document: HTTP 200, or unknown status for uncrawled URLs."""
        if self.is_redirect or self.is_broken:
            return False
        return self.status in (0, 200)

    @property
    def robots_directives(self) -> set[str]:
        combined = f"{self.meta_robots},{self.x_robots}".lower()
        return {part.strip() for part in combined.split(",") if part.strip()}

    @property
    def noindex(self) -> bool:
        directives = self.robots_directives
        return "noindex" in directives or "none" in directives

    @property
    def nofollow(self) -> bool:
        directives = self.robots_directives
        return "nofollow" in directives or "none" in directives

    @property
    def canonicalized(self) -> bool:
        """True when the canonical tag points at a different URL."""
        return bool(self.canonical) and self.canonical != self.url

    @property
    def indexable(self) -> bool:
        return self.is_ok and not self.noindex and not self.canonicalized

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"url": self.url, "status": self.status}
        for name in (
            "title",
            "h1",
            "word_count",
            "canonical",
            "meta_robots",
            "x_robots",
            "content_type",
            "redirect_target",
            "redirect_chain",
            "in_sitemap",
            "crawled",
            "blocked_by_robots",
            "error",
            "text",
        ):
            value = getattr(self, name)
            if value:
                data[name] = value
        if self.depth is not None:
            data["depth"] = self.depth
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Page:
        depth = data.get("depth")
        return cls(
            url=data["url"],
            status=int(data.get("status", 0) or 0),
            title=data.get("title", "") or "",
            h1=data.get("h1", "") or "",
            word_count=int(data.get("word_count", 0) or 0),
            canonical=data.get("canonical", "") or "",
            meta_robots=data.get("meta_robots", "") or "",
            x_robots=data.get("x_robots", "") or "",
            content_type=data.get("content_type", "") or "",
            depth=int(depth) if depth is not None else None,
            redirect_target=data.get("redirect_target", "") or "",
            redirect_chain=list(data.get("redirect_chain") or []),
            in_sitemap=bool(data.get("in_sitemap", False)),
            crawled=bool(data.get("crawled", False)),
            blocked_by_robots=bool(data.get("blocked_by_robots", False)),
            error=data.get("error", "") or "",
            text=data.get("text", "") or "",
        )


@dataclass
class SiteData:
    """The raw material of an analysis: pages, links and sitemap membership."""

    start_url: str = ""
    pages: dict[str, Page] = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)
    sitemap_urls: set[str] = field(default_factory=set)
    meta: dict[str, Any] = field(default_factory=dict)

    def page(self, url: str) -> Page:
        """Return the page for ``url``, creating an empty record if needed."""
        existing = self.pages.get(url)
        if existing is None:
            existing = Page(url=url)
            self.pages[url] = existing
        return existing

    def add_link(self, link: Link) -> None:
        self.links.append(link)

    def internal_links(self) -> Iterator[Link]:
        return (link for link in self.links if link.internal)

    def external_links(self) -> Iterator[Link]:
        return (link for link in self.links if not link.internal)

    def resolve_redirect(self, url: str, max_hops: int = 20) -> str:
        """Follow recorded redirects starting at ``url`` and return the final URL."""
        seen = {url}
        current = url
        for _ in range(max_hops):
            page = self.pages.get(current)
            if page is None or not page.redirect_target:
                break
            nxt = page.redirect_target
            if nxt in seen:
                break
            seen.add(nxt)
            current = nxt
        return current

    def ensure_link_targets(self) -> None:
        """Make sure every internal link endpoint has a :class:`Page` record."""
        for link in self.links:
            self.page(link.source)
            if link.internal:
                self.page(link.target)
        for url in self.sitemap_urls:
            self.page(url).in_sitemap = True

    def to_dict(self, include_text: bool = True) -> dict[str, Any]:
        pages = []
        for url in sorted(self.pages):
            data = self.pages[url].to_dict()
            if not include_text:
                data.pop("text", None)
            pages.append(data)
        return {
            "format": "internal-link-graph/crawl",
            "version": FORMAT_VERSION,
            "start_url": self.start_url,
            "meta": self.meta,
            "sitemap_urls": sorted(self.sitemap_urls),
            "pages": pages,
            "links": [link.to_dict() for link in self.links],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteData:
        site = cls(start_url=data.get("start_url", "") or "", meta=dict(data.get("meta") or {}))
        for item in data.get("pages") or []:
            page = Page.from_dict(item)
            site.pages[page.url] = page
        site.links = [Link.from_dict(item) for item in data.get("links") or []]
        site.sitemap_urls = set(data.get("sitemap_urls") or [])
        site.ensure_link_targets()
        return site

    def save(self, path: str | Path, include_text: bool = True) -> None:
        payload = json.dumps(self.to_dict(include_text=include_text), ensure_ascii=False, indent=1)
        Path(path).write_text(payload + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> SiteData:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def unique_edges(links: Iterable[Link]) -> set[tuple[str, str]]:
    """Distinct ``(source, target)`` pairs of internal links."""
    return {(link.source, link.target) for link in links if link.internal}
