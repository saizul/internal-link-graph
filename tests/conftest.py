"""Shared fixtures: an in-memory website served by ``DictFetcher``."""

from __future__ import annotations

import pytest

from internal_link_graph.fetcher import DictFetcher
from internal_link_graph.models import Link, Page, SiteData

BASE = "https://example.com"


def html_page(title: str, links: list[tuple[str, str]] | None = None, body: str = "",
              head: str = "", h1: str | None = None) -> str:
    """Build a small HTML document with the given ``(href, anchor)`` links."""
    anchors = "".join(f'<a href="{href}">{text}</a> ' for href, text in links or [])
    heading = title if h1 is None else h1
    return (
        f"<!doctype html><html><head><title>{title}</title>{head}</head>"
        f"<body><main><h1>{heading}</h1><p>{body}</p>{anchors}</main></body></html>"
    )


def make_site_map() -> dict[str, object]:
    """A small site with a redirect, a 404, an orphan (sitemap only) and a deep page."""
    return {
        f"{BASE}/": html_page(
            "Home",
            [("/blog/", "Blog"), ("/about", "About"), ("/old", "Old page"), ("/missing", "Gone")],
            "Welcome to the example site about gardening tools.",
        ),
        f"{BASE}/blog/": html_page(
            "Blog",
            [("/", "Home"), ("/blog/pruning-roses", "Pruning roses"),
             ("/blog/compost-basics", "Compost basics")],
            "Articles about gardening.",
        ),
        f"{BASE}/blog/pruning-roses": html_page(
            "Pruning roses in spring",
            [("/blog/", "Blog"), ("/blog/rose-diseases", "click here")],
            "How to prune roses with sharp secateurs in spring for healthy rose bushes.",
        ),
        f"{BASE}/blog/rose-diseases": html_page(
            "Common rose diseases",
            [("https://other.example.org/roses", "External reference")],
            "Black spot and mildew are rose diseases that damage rose bushes after pruning.",
        ),
        f"{BASE}/blog/compost-basics": html_page(
            "Compost basics",
            [("/blog/", "Blog")],
            "Making compost from kitchen scraps and garden waste.",
        ),
        f"{BASE}/about": html_page("About", [("/", "Home")], "About the gardeners."),
        f"{BASE}/old": (301, {"Location": "/about"}, ""),
        f"{BASE}/orphan": html_page("Orphan", [("/", "Home")], "Nobody links here."),
        f"{BASE}/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n"),
        f"{BASE}/sitemap.xml": (
            200,
            {"Content-Type": "application/xml"},
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<url><loc>{BASE}/</loc></url><url><loc>{BASE}/orphan</loc></url></urlset>",
        ),
    }


@pytest.fixture
def site_map() -> dict[str, object]:
    return make_site_map()


@pytest.fixture
def fetcher(site_map) -> DictFetcher:
    return DictFetcher(site_map)


def build_site(edges: list[tuple[str, str]], start: str = "", **page_kwargs) -> SiteData:
    """SiteData from bare ``(source, target)`` pairs; short names become example.com URLs."""

    def url(name: str) -> str:
        return name if "://" in name else f"{BASE}/{name}".replace("/home", "/")

    site = SiteData(start_url=url(start) if start else "")
    for source, target in edges:
        site.add_link(Link(source=url(source), target=url(target), anchor=target))
    site.ensure_link_targets()
    for name, kwargs in page_kwargs.items():
        page = site.page(url(name))
        for key, value in kwargs.items():
            setattr(page, key, value)
    return site


@pytest.fixture
def small_site() -> SiteData:
    """home -> a, b; a -> b, c; b -> home; c -> a; plus orphan o -> a."""
    site = build_site(
        [("home", "a"), ("home", "b"), ("a", "b"), ("a", "c"), ("b", "home"), ("c", "a"),
         ("o", "a")],
        start="home",
    )
    for page in site.pages.values():
        page.status = 200
        page.crawled = True
        page.title = f"Title of {page.url.rsplit('/', 1)[-1] or 'home'}"
        page.h1 = page.title
    return site


__all__ = ["BASE", "Page", "build_site", "html_page", "make_site_map"]
