import pytest

from conftest import BASE, html_page
from internal_link_graph.crawler import CrawlConfig, Crawler, CrawlError, crawl
from internal_link_graph.fetcher import DictFetcher, Response
from internal_link_graph.ratelimit import HostRateLimiter

HOME = f"{BASE}/"


def run(fetcher, **options):
    options.setdefault("delay", 0)
    options.setdefault("workers", 1)
    crawler = Crawler(CrawlConfig(HOME, **options), fetcher=fetcher)
    return crawler.crawl(), crawler


def test_full_crawl_collects_pages_links_and_depths(fetcher):
    site, crawler = run(fetcher)
    assert site.start_url == HOME
    assert site.pages[HOME].depth == 0
    assert site.pages[f"{BASE}/blog/"].depth == 1
    assert site.pages[f"{BASE}/blog/pruning-roses"].depth == 2
    assert site.pages[f"{BASE}/blog/rose-diseases"].depth == 3
    assert site.pages[f"{BASE}/blog/pruning-roses"].title == "Pruning roses in spring"
    assert site.pages[HOME].word_count > 5
    assert crawler.stats.pages_crawled == 8
    assert site.meta["source"] == "crawl"
    assert site.meta["stats"]["errors"] == 1


def test_links_carry_anchor_position_and_internal_flag(fetcher):
    site, _ = run(fetcher)
    by_pair = {(link.source, link.target): link for link in site.links}
    blog = by_pair[(HOME, f"{BASE}/blog/")]
    assert (blog.anchor, blog.position, blog.internal) == ("Blog", "main", True)
    external = by_pair[(f"{BASE}/blog/rose-diseases", "https://other.example.org/roses")]
    assert external.internal is False
    assert "https://other.example.org/roses" not in site.pages


def test_external_pages_are_never_fetched(fetcher):
    run(fetcher)
    assert not [url for url in fetcher.requests if "other.example.org" in url]


def test_redirect_is_recorded_with_chain(fetcher):
    site, crawler = run(fetcher)
    old = site.pages[f"{BASE}/old"]
    assert old.status == 301
    assert old.redirect_target == f"{BASE}/about"
    assert old.redirect_chain == [f"{BASE}/old", f"{BASE}/about"]
    assert crawler.stats.redirects == 1
    assert site.resolve_redirect(f"{BASE}/old") == f"{BASE}/about"


def test_multi_hop_redirect_chain(site_map):
    site_map[f"{BASE}/old"] = (301, {"Location": "/older"}, "")
    site_map[f"{BASE}/older"] = (302, {"Location": "/about"}, "")
    site, _ = run(DictFetcher(site_map))
    assert site.pages[f"{BASE}/old"].redirect_chain == [
        f"{BASE}/old", f"{BASE}/older", f"{BASE}/about"
    ]
    assert site.resolve_redirect(f"{BASE}/old") == f"{BASE}/about"
    assert site.pages[f"{BASE}/about"].status == 200


def test_redirect_loop_terminates(site_map):
    site_map[f"{BASE}/old"] = (301, {"Location": "/loop"}, "")
    site_map[f"{BASE}/loop"] = (301, {"Location": "/old"}, "")
    site, _ = run(DictFetcher(site_map))
    assert site.pages[f"{BASE}/old"].is_redirect


def test_redirect_to_external_site_is_not_followed(site_map):
    site_map[f"{BASE}/old"] = (301, {"Location": "https://other.example.org/landing"}, "")
    fetcher = DictFetcher(site_map)
    site, _ = run(fetcher)
    assert site.pages[f"{BASE}/old"].status == 301
    assert "https://other.example.org/landing" not in fetcher.requests


def test_broken_page_is_recorded(fetcher):
    site, _ = run(fetcher)
    missing = site.pages[f"{BASE}/missing"]
    assert missing.status == 404 and missing.is_broken and missing.crawled


def test_network_error_is_recorded(site_map):
    site_map[f"{BASE}/about"] = Response("x", status=0, error="timeout")
    site, crawler = run(DictFetcher(site_map))
    about = site.pages[f"{BASE}/about"]
    assert about.status == 0 and about.error == "timeout" and about.is_broken
    assert crawler.stats.errors == 2


def test_sitemap_seeding_finds_orphans(fetcher):
    site, crawler = run(fetcher)
    orphan = site.pages[f"{BASE}/orphan"]
    assert orphan.crawled and orphan.in_sitemap and orphan.status == 200
    assert site.sitemap_urls == {HOME, f"{BASE}/orphan"}
    assert crawler.stats.sitemap_urls == 2


def test_no_sitemap_option_skips_seeding(fetcher):
    site, _ = run(fetcher, use_sitemap=False)
    assert f"{BASE}/orphan" not in site.pages
    assert f"{BASE}/sitemap.xml" not in fetcher.requests
    assert site.sitemap_urls == set()


def test_sitemap_declared_in_robots_txt_is_used(site_map):
    del site_map[f"{BASE}/sitemap.xml"]
    site_map[f"{BASE}/robots.txt"] = (
        200, {"Content-Type": "text/plain"},
        f"User-agent: *\nAllow: /\nSitemap: {BASE}/custom-map.xml\n",
    )
    site_map[f"{BASE}/custom-map.xml"] = (
        200, {"Content-Type": "application/xml"},
        f"<urlset><url><loc>{BASE}/orphan</loc></url></urlset>",
    )
    site, _ = run(DictFetcher(site_map))
    assert site.pages[f"{BASE}/orphan"].crawled


def test_explicit_sitemap_urls(site_map):
    site_map[f"{BASE}/extra.xml"] = (
        200, {"Content-Type": "application/xml"},
        f"<urlset><url><loc>{BASE}/hidden</loc></url></urlset>",
    )
    site_map[f"{BASE}/hidden"] = html_page("Hidden")
    site, _ = run(DictFetcher(site_map), sitemap_urls=(f"{BASE}/extra.xml",))
    assert site.pages[f"{BASE}/hidden"].in_sitemap


def test_robots_disallow_blocks_fetching(site_map):
    site_map[f"{BASE}/robots.txt"] = (
        200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /blog/\n"
    )
    fetcher = DictFetcher(site_map)
    site, crawler = run(fetcher)
    blog = site.pages[f"{BASE}/blog/"]
    assert blog.blocked_by_robots and not blog.is_broken
    assert f"{BASE}/blog/" not in fetcher.requests
    assert f"{BASE}/blog/pruning-roses" not in site.pages
    assert crawler.stats.blocked_by_robots == 1


def test_ignore_robots_fetches_everything(site_map):
    site_map[f"{BASE}/robots.txt"] = (
        200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /\n"
    )
    fetcher = DictFetcher(site_map)
    site, _ = run(fetcher, respect_robots=False)
    assert site.pages[f"{BASE}/blog/pruning-roses"].crawled


def test_max_pages_limits_fetches_but_keeps_discovered_urls(fetcher):
    site, crawler = run(fetcher, max_pages=2, use_sitemap=False)
    crawled = [page for page in site.pages.values() if page.crawled]
    assert len(crawled) == 2
    assert crawler.stats.pages_crawled == 2
    assert crawler.stats.frontier_remaining > 0
    assert len(site.pages) > 2  # link targets are known even if not fetched


def test_max_depth_limits_crawl(fetcher):
    site, _ = run(fetcher, max_depth=1, use_sitemap=False)
    assert site.pages[f"{BASE}/blog/"].crawled
    assert not site.pages[f"{BASE}/blog/pruning-roses"].crawled
    assert f"{BASE}/blog/rose-diseases" not in site.pages


def test_exclude_pattern(fetcher):
    site, crawler = run(fetcher, exclude=(r"/blog/",))
    assert not any("/blog/" in url for url in fetcher.requests)
    assert not crawler.allowed_by_filters(f"{BASE}/blog/x")
    assert crawler.allowed_by_filters(f"{BASE}/about")
    assert site.pages[f"{BASE}/about"].crawled


def test_include_pattern_restricts_crawl_but_not_start(fetcher):
    site, _ = run(fetcher, include=(r"/blog/",), use_sitemap=False)
    assert site.pages[HOME].crawled
    assert site.pages[f"{BASE}/blog/compost-basics"].crawled
    assert f"{BASE}/about" not in fetcher.requests


def test_invalid_patterns_and_start_urls_raise():
    with pytest.raises(CrawlError):
        Crawler(CrawlConfig(HOME, exclude=("(",)), fetcher=DictFetcher())
    with pytest.raises(CrawlError):
        Crawler(CrawlConfig("ftp://example.com/"), fetcher=DictFetcher())
    with pytest.raises(CrawlError):
        Crawler(CrawlConfig("not a url"), fetcher=DictFetcher())


def test_non_html_resources_are_not_parsed(site_map):
    site_map[HOME] = html_page("Home", [("/api/data", "Data"), ("/brochure.pdf", "PDF")])
    site_map[f"{BASE}/api/data"] = (
        200, {"Content-Type": "application/json"}, '{"a": "<a href=/secret>x</a>"}'
    )
    fetcher = DictFetcher(site_map)
    site, _ = run(fetcher, use_sitemap=False)
    assert site.pages[f"{BASE}/api/data"].content_type == "application/json"
    assert f"{BASE}/secret" not in site.pages
    assert f"{BASE}/brochure.pdf" not in fetcher.requests  # skipped by extension


def test_canonical_meta_robots_and_x_robots_are_stored(site_map):
    site_map[f"{BASE}/about"] = (
        200,
        {"Content-Type": "text/html", "X-Robots-Tag": "noarchive"},
        html_page("About", head='<link rel="canonical" href="/about-us">'
                                '<meta name="robots" content="noindex">'),
    )
    site, _ = run(DictFetcher(site_map))
    about = site.pages[f"{BASE}/about"]
    assert about.canonical == f"{BASE}/about-us"
    assert about.noindex and not about.indexable
    assert "noarchive" in about.x_robots


def test_page_level_nofollow_marks_links(site_map):
    site_map[f"{BASE}/about"] = html_page(
        "About", [("/", "Home")], head='<meta name="robots" content="nofollow">'
    )
    site, _ = run(DictFetcher(site_map))
    link = next(link for link in site.links if link.source == f"{BASE}/about")
    assert link.nofollow


def test_follow_nofollow_false_does_not_enqueue_nofollow_links(site_map):
    site_map[HOME] = (
        '<html><body><a href="/about" rel="nofollow">About</a><a href="/blog/">Blog</a></body>'
        "</html>"
    )
    fetcher = DictFetcher(site_map)
    site, _ = run(fetcher, follow_nofollow=False, use_sitemap=False)
    assert f"{BASE}/about" not in fetcher.requests
    assert any(link.target == f"{BASE}/about" and link.nofollow for link in site.links)


def test_subdomains_are_external_unless_enabled(site_map):
    site_map[HOME] = html_page("Home", [("https://shop.example.com/", "Shop")])
    site_map["https://shop.example.com/"] = html_page("Shop")
    site, _ = run(DictFetcher(site_map), use_sitemap=False)
    assert not site.links[0].internal
    site, _ = run(DictFetcher(site_map), use_sitemap=False, include_subdomains=True)
    assert site.links[0].internal
    assert site.pages["https://shop.example.com/"].crawled


def test_start_url_redirect_moves_the_start(site_map):
    site_map["https://example.com/start"] = (301, {"Location": "/"}, "")
    crawler = Crawler(
        CrawlConfig("https://example.com/start", delay=0, workers=1), fetcher=DictFetcher(site_map)
    )
    site = crawler.crawl()
    assert site.start_url == HOME
    assert site.pages[HOME].depth == 0


def test_each_url_is_fetched_once(fetcher):
    run(fetcher)
    pages = [url for url in fetcher.requests if not url.endswith((".txt", ".xml"))]
    # /about is fetched as a page and once more as the redirect target of /old
    assert all(fetcher.count(url) == 1 for url in pages if not url.endswith("/about"))


def test_multiple_workers_give_same_result(site_map):
    single, _ = run(DictFetcher(site_map), workers=1)
    multi, _ = run(DictFetcher(site_map), workers=4)
    assert set(single.pages) == set(multi.pages)
    assert {(link.source, link.target) for link in single.links} == {
        (link.source, link.target) for link in multi.links
    }
    assert {u: p.status for u, p in single.pages.items()} == {
        u: p.status for u, p in multi.pages.items()
    }


def test_rate_limiter_is_used_for_every_page(fetcher):
    waits = []

    class Recorder(HostRateLimiter):
        def wait(self, host, delay=None):
            waits.append(host)
            return 0.0

    Crawler(CrawlConfig(HOME, delay=0, workers=1), fetcher=fetcher,
            rate_limiter=Recorder(0)).crawl()
    assert waits and set(waits) == {"example.com"}
    assert len(waits) >= 8


def test_progress_callback_is_invoked(fetcher):
    events = []
    Crawler(CrawlConfig(HOME, delay=0, workers=1), fetcher=fetcher,
            progress=lambda *args: events.append(args)).crawl()
    assert len(events) >= 8


def test_state_file_and_resume(site_map, tmp_path):
    state = tmp_path / "crawl.sqlite"
    first_fetcher = DictFetcher(site_map)
    partial, first = run(first_fetcher, max_pages=3, state_path=str(state))
    assert first.stats.pages_crawled == 3
    assert state.exists()

    second_fetcher = DictFetcher(site_map)
    resumed, second = run(second_fetcher, state_path=str(state), resume=True)
    assert second.stats.resumed
    full, _ = run(DictFetcher(site_map))
    assert set(resumed.pages) == set(full.pages)
    assert {u: p.status for u, p in resumed.pages.items()} == {
        u: p.status for u, p in full.pages.items()
    }
    assert {(link.source, link.target) for link in resumed.links} == {
        (link.source, link.target) for link in full.links
    }
    # /about is legitimately requested again as the redirect target of /old
    already = {
        url for url, page in partial.pages.items()
        if page.crawled and page.status == 200 and not url.endswith("/about")
    }
    assert already and not (already & set(second_fetcher.requests))


def test_resume_without_existing_state_starts_fresh(fetcher, tmp_path):
    site, crawler = run(fetcher, state_path=str(tmp_path / "new.sqlite"), resume=True)
    assert not crawler.stats.resumed
    assert crawler.stats.pages_crawled == 8 and len(site.pages) == 9


def test_state_is_cleared_without_resume(site_map, tmp_path):
    state = str(tmp_path / "crawl.sqlite")
    run(DictFetcher(site_map), max_pages=2, state_path=state)
    fetcher = DictFetcher(site_map)
    _, crawler = run(fetcher, state_path=state)
    assert not crawler.stats.resumed
    assert fetcher.count(HOME) == 1


def test_crawl_convenience_function(fetcher):
    site = crawl(HOME, fetcher=fetcher, delay=0, workers=1, max_pages=4)
    assert site.meta["stats"]["pages_crawled"] == 4
    assert site.meta["max_pages"] == 4
    assert not site.pages[f"{BASE}/blog/pruning-roses"].crawled


def test_text_is_capped_and_can_be_disabled(fetcher):
    site, _ = run(fetcher, max_text_chars=10)
    assert all(len(page.text) <= 10 for page in site.pages.values())
    site, _ = run(DictFetcher(fetcher.site), max_text_chars=0)
    assert all(page.text == "" for page in site.pages.values())
