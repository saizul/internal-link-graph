import gzip

import pytest

from internal_link_graph.fetcher import DictFetcher
from internal_link_graph.ratelimit import HostRateLimiter
from internal_link_graph.robots import RobotsCache, parse_robots, rules_for_response
from internal_link_graph.sitemap import (
    SitemapError,
    collect_sitemap_urls,
    maybe_gunzip,
    parse_sitemap,
)

ROBOTS = """
User-agent: *
Disallow: /private/
Crawl-delay: 2

User-agent: nicebot
Disallow:

Sitemap: https://example.com/sitemap.xml
Sitemap: https://example.com/news.xml
"""

URLSET = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    b"<url><loc> https://example.com/a </loc><lastmod>2026-01-01</lastmod></url>"
    b"<url><loc>https://example.com/b</loc></url><url></url></urlset>"
)


def test_parse_robots_rules_delay_and_sitemaps():
    rules = parse_robots(ROBOTS)
    assert not rules.can_fetch("anybot", "https://example.com/private/x")
    assert rules.can_fetch("anybot", "https://example.com/public")
    assert rules.can_fetch("nicebot", "https://example.com/private/x")
    assert rules.crawl_delay("anybot") == 2
    assert rules.crawl_delay("nicebot") is None
    assert rules.sitemaps == ["https://example.com/sitemap.xml", "https://example.com/news.xml"]


@pytest.mark.parametrize(
    ("status", "allowed"),
    [(200, True), (404, True), (410, True), (401, False), (403, False), (500, False), (0, False)],
)
def test_rules_for_response_status_policy(status, allowed):
    rules = rules_for_response(status, b"User-agent: *\nAllow: /\n")
    assert rules.can_fetch("bot", "https://example.com/x") is allowed


def test_robots_cache_fetches_once_per_origin():
    fetcher = DictFetcher(
        {"https://example.com/robots.txt": (200, {"content-type": "text/plain"}, ROBOTS)}
    )
    cache = RobotsCache(fetcher, "anybot")
    assert not cache.allowed("https://example.com/private/a")
    assert cache.allowed("https://example.com/b")
    assert cache.crawl_delay("https://example.com/b") == 2
    assert cache.sitemaps("https://example.com/")[0].endswith("sitemap.xml")
    assert fetcher.count("https://example.com/robots.txt") == 1
    assert cache.allowed("https://other.example.org/private/a")  # 404 -> allow all
    assert fetcher.requests[-1] == "https://other.example.org/robots.txt"


def test_robots_cache_follows_redirected_robots_txt():
    fetcher = DictFetcher(
        {
            "https://example.com/robots.txt": (301, {"Location": "/real-robots.txt"}, ""),
            "https://example.com/real-robots.txt": (200, {"content-type": "text/plain"}, ROBOTS),
        }
    )
    assert not RobotsCache(fetcher, "bot").allowed("https://example.com/private/")


def test_robots_cache_disabled_allows_everything_without_fetching():
    fetcher = DictFetcher({"https://example.com/robots.txt": "User-agent: *\nDisallow: /\n"})
    cache = RobotsCache(fetcher, "bot", enabled=False)
    assert cache.allowed("https://example.com/anything")
    assert cache.crawl_delay("https://example.com/") is None
    assert fetcher.requests == []


def test_parse_urlset():
    document = parse_sitemap(URLSET)
    assert document.kind == "urlset"
    assert document.urls == ["https://example.com/a", "https://example.com/b"]


def test_parse_sitemap_index_and_plain_text():
    index = parse_sitemap(
        b"<sitemapindex><sitemap><loc>https://example.com/s1.xml</loc></sitemap></sitemapindex>"
    )
    assert (index.kind, index.urls) == ("index", ["https://example.com/s1.xml"])
    text = parse_sitemap(b"https://example.com/a\n# comment\nhttps://example.com/b\n")
    assert (text.kind, text.urls) == ("text", ["https://example.com/a", "https://example.com/b"])


def test_parse_gzipped_sitemap():
    assert parse_sitemap(gzip.compress(URLSET)).urls[0] == "https://example.com/a"
    assert maybe_gunzip(b"plain") == b"plain"
    with pytest.raises(SitemapError):
        maybe_gunzip(b"\x1f\x8b\x08garbage-not-gzip")


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"   ",
        b"<urlset><url>",
        b"<html><body>not a sitemap</body></html>",
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><urlset></urlset>',
        b"just some words",
    ],
)
def test_parse_sitemap_rejects_bad_documents(data):
    with pytest.raises(SitemapError):
        parse_sitemap(data)


def test_collect_sitemap_urls_walks_indexes_and_dedupes():
    xml = {"content-type": "application/xml"}
    fetcher = DictFetcher(
        {
            "https://example.com/index.xml": (
                200, xml,
                b"<sitemapindex><sitemap><loc>https://example.com/s1.xml.gz</loc></sitemap>"
                b"<sitemap><loc>https://example.com/s2.xml</loc></sitemap>"
                b"<sitemap><loc>https://example.com/index.xml</loc></sitemap>"
                b"<sitemap><loc>https://example.com/missing.xml</loc></sitemap></sitemapindex>",
            ),
            "https://example.com/s1.xml.gz": (200, xml, gzip.compress(URLSET)),
            "https://example.com/s2.xml": (
                200, xml,
                b"<urlset><url><loc>https://example.com/b</loc></url>"
                b"<url><loc>https://example.com/c</loc></url></urlset>",
            ),
        }
    )
    result = collect_sitemap_urls(fetcher, ["https://example.com/index.xml"])
    assert result.urls == ["https://example.com/a", "https://example.com/b",
                           "https://example.com/c"]
    assert len(result.fetched) == 3
    assert result.errors == {"https://example.com/missing.xml": "HTTP 404"}
    assert fetcher.count("https://example.com/index.xml") == 1


def test_collect_sitemap_urls_limits():
    body = b"<urlset>" + b"".join(
        f"<url><loc>https://example.com/{i}</loc></url>".encode() for i in range(20)
    ) + b"</urlset>"
    fetcher = DictFetcher({"https://example.com/s.xml": (200, {}, body),
                           "https://example.com/t.xml": (200, {}, body)})
    assert len(collect_sitemap_urls(fetcher, ["https://example.com/s.xml"], max_urls=5).urls) == 5
    limited = collect_sitemap_urls(
        fetcher, ["https://example.com/s.xml", "https://example.com/t.xml"], max_sitemaps=1
    )
    assert limited.fetched == ["https://example.com/s.xml"]


def test_collect_sitemap_reports_parse_errors():
    fetcher = DictFetcher({"https://example.com/s.xml": (200, {}, b"<oops")})
    result = collect_sitemap_urls(fetcher, ["https://example.com/s.xml"])
    assert result.urls == [] and "invalid XML" in result.errors["https://example.com/s.xml"]


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_rate_limiter_spaces_requests_per_host():
    fake = FakeClock()
    limiter = HostRateLimiter(1.0, clock=fake.clock, sleep=fake.sleep)
    assert limiter.wait("a.example") == 0
    assert limiter.wait("a.example") == pytest.approx(1.0)
    assert limiter.wait("b.example") == 0  # other hosts are independent
    fake.now += 5
    assert limiter.wait("a.example") == 0  # enough time has passed
    assert fake.slept == [pytest.approx(1.0)]
    assert limiter.total_waited == pytest.approx(1.0)


def test_rate_limiter_reservations_queue_up():
    fake = FakeClock()
    limiter = HostRateLimiter(0.5, clock=fake.clock, sleep=fake.sleep)
    waits = [limiter.reserve("h") for _ in range(4)]
    assert waits == pytest.approx([0, 0.5, 1.0, 1.5])


def test_rate_limiter_per_call_delay_and_validation():
    fake = FakeClock()
    limiter = HostRateLimiter(0, clock=fake.clock, sleep=fake.sleep)
    limiter.wait("h", delay=3)
    assert limiter.wait("h") == pytest.approx(3)
    with pytest.raises(ValueError):
        HostRateLimiter(-1)
