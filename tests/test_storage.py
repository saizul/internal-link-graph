from internal_link_graph.models import Link, Page
from internal_link_graph.storage import CrawlStore


def test_store_starts_empty(tmp_path):
    with CrawlStore(tmp_path / "state.sqlite") as store:
        assert not store.has_state()
        assert store.page_count() == 0 and store.link_count() == 0
        assert store.get_meta("missing") is None
        assert store.get_meta("missing", "fallback") == "fallback"


def test_meta_roundtrip_and_overwrite(tmp_path):
    with CrawlStore(tmp_path / "state.sqlite") as store:
        store.set_meta("start_url", "https://example.com/")
        store.set_meta("start_url", "https://example.org/")
        assert store.get_meta("start_url") == "https://example.org/"


def test_pages_and_links_persist_across_connections(tmp_path):
    path = tmp_path / "state.sqlite"
    with CrawlStore(path) as store:
        store.set_meta("start_url", "https://example.com/")
        store.save_page(Page("https://example.com/", status=200, title="Home", depth=0,
                             crawled=True, text="hello"))
        store.save_page(Page("https://example.com/", status=200, title="Home v2", depth=0))
        store.save_links(
            "https://example.com/",
            [Link("https://example.com/", "https://example.com/a", anchor="A", rel=("nofollow",),
                  nofollow=True, position="nav")],
        )
        store.commit()
    with CrawlStore(path) as store:
        assert store.has_state()
        assert store.page_count() == 1 and store.link_count() == 1
        site = store.load_site()
    assert site.start_url == "https://example.com/"
    assert site.pages["https://example.com/"].title == "Home v2"
    link = site.links[0]
    assert (link.anchor, link.rel, link.nofollow, link.position) == ("A", ("nofollow",), True,
                                                                      "nav")


def test_save_links_replaces_previous_links_of_source(tmp_path):
    with CrawlStore(tmp_path / "s.sqlite") as store:
        store.save_links("a", [Link("a", "b"), Link("a", "c")])
        store.save_links("a", [Link("a", "d")])
        assert store.link_count() == 1


def test_frontier_order_priorities_and_removal(tmp_path):
    with CrawlStore(tmp_path / "s.sqlite") as store:
        store.add_frontier([("u1", 1), ("u2", 1), ("u3", 2)])
        store.add_frontier([("s1", 0)], priority=1)
        store.add_frontier([("u1", 5)])  # duplicates are ignored
        assert store.load_frontier() == [("u1", 1), ("u2", 1), ("u3", 2)]
        assert store.load_frontier(priority=1) == [("s1", 0)]
        assert store.frontier_size() == 4
        store.remove_frontier("u2")
        assert [url for url, _ in store.load_frontier()] == ["u1", "u3"]


def test_seen_and_sitemap_sets(tmp_path):
    with CrawlStore(tmp_path / "s.sqlite") as store:
        store.mark_seen(["a", "b", "a"])
        store.add_sitemap_urls(["x", "x", "y"])
        assert store.load_seen() == {"a", "b"}
        assert store.load_sitemap_urls() == {"x", "y"}
        assert store.load_site().sitemap_urls == {"x", "y"}


def test_clear_removes_everything(tmp_path):
    with CrawlStore(tmp_path / "s.sqlite") as store:
        store.set_meta("k", "v")
        store.save_page(Page("a", status=200))
        store.add_frontier([("b", 1)])
        store.mark_seen(["a"])
        store.clear()
        assert not store.has_state()
        assert store.get_meta("k") is None and store.frontier_size() == 0
