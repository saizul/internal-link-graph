from internal_link_graph.models import Link, Page, SiteData, unique_edges


def test_link_roundtrip_minimal_and_full():
    minimal = Link(source="a", target="b")
    assert minimal.to_dict() == {"source": "a", "target": "b"}
    full = Link("a", "b", anchor="B", rel=("nofollow",), position="nav", is_image=True,
                internal=False, nofollow=True)
    assert Link.from_dict(full.to_dict()) == full
    assert full.follow is False and minimal.follow is True


def test_link_from_dict_derives_nofollow_from_rel_string():
    link = Link.from_dict({"source": "a", "target": "b", "rel": "UGC noopener"})
    assert link.rel == ("ugc", "noopener")
    assert link.nofollow is True


def test_page_status_properties():
    assert Page("u", status=200).is_ok
    assert Page("u").is_ok  # unknown status counts as a normal document
    assert Page("u", status=301, redirect_target="v").is_redirect
    assert not Page("u", status=301).is_ok
    assert Page("u", status=404).is_broken
    assert Page("u", status=503).is_broken
    assert Page("u", status=0, crawled=True, error="timeout").is_broken
    assert not Page("u", status=0, crawled=True, error="robots", blocked_by_robots=True).is_broken
    assert not Page("u", status=204).is_ok


def test_page_robots_directives_and_indexability():
    page = Page("u", status=200, meta_robots="noindex,follow", x_robots="NOFOLLOW")
    assert page.robots_directives == {"noindex", "follow", "nofollow"}
    assert page.noindex and page.nofollow and not page.indexable
    assert Page("u", status=200, meta_robots="none").noindex
    assert Page("u", status=200).indexable


def test_canonicalized_page_is_not_indexable():
    assert not Page("u", status=200, canonical="u").canonicalized
    page = Page("u", status=200, canonical="other")
    assert page.canonicalized and not page.indexable


def test_page_roundtrip_omits_defaults():
    page = Page("u", status=200, title="T", depth=0, redirect_chain=["a", "b"], in_sitemap=True)
    data = page.to_dict()
    assert "h1" not in data and data["depth"] == 0
    assert Page.from_dict(data) == page
    assert Page.from_dict({"url": "x"}).depth is None


def test_site_page_is_get_or_create():
    site = SiteData()
    first = site.page("u")
    first.title = "T"
    assert site.page("u") is first
    assert len(site.pages) == 1


def test_resolve_redirect_follows_chain_and_survives_loops():
    site = SiteData()
    site.page("a").redirect_target = "b"
    site.page("b").redirect_target = "c"
    site.page("c")
    assert site.resolve_redirect("a") == "c"
    assert site.resolve_redirect("unknown") == "unknown"
    site.page("x").redirect_target = "y"
    site.page("y").redirect_target = "x"
    assert site.resolve_redirect("x") in {"x", "y"}


def test_ensure_link_targets_creates_pages_and_marks_sitemap():
    site = SiteData(sitemap_urls={"s"})
    site.add_link(Link("a", "b"))
    site.add_link(Link("a", "https://external.example.org/", internal=False))
    site.ensure_link_targets()
    assert set(site.pages) == {"a", "b", "s"}
    assert site.pages["s"].in_sitemap


def test_internal_and_external_link_views():
    site = SiteData(links=[Link("a", "b"), Link("a", "x", internal=False), Link("a", "b")])
    assert len(list(site.internal_links())) == 2
    assert len(list(site.external_links())) == 1
    assert unique_edges(site.links) == {("a", "b")}


def test_site_save_and_load_roundtrip(tmp_path):
    site = SiteData(start_url="https://example.com/", meta={"source": "test"})
    site.page("https://example.com/").text = "body text"
    site.pages["https://example.com/"].status = 200
    site.add_link(Link("https://example.com/", "https://example.com/a", anchor="এ"))
    site.sitemap_urls.add("https://example.com/a")
    site.ensure_link_targets()
    path = tmp_path / "crawl.json"
    site.save(path)
    loaded = SiteData.load(path)
    assert loaded.to_dict() == site.to_dict()
    assert loaded.links[0].anchor == "এ"
    site.save(path, include_text=False)
    assert SiteData.load(path).pages["https://example.com/"].text == ""


def test_to_dict_format_marker_and_sorted_pages():
    site = SiteData()
    site.page("b")
    site.page("a")
    data = site.to_dict()
    assert data["format"] == "internal-link-graph/crawl"
    assert [page["url"] for page in data["pages"]] == ["a", "b"]
