import json

import pytest

from conftest import BASE, build_site
from internal_link_graph.analyzer import (
    ISSUE_TYPES,
    SEVERITIES,
    AnalysisConfig,
    Issue,
    analyze,
    choose_start_url,
)
from internal_link_graph.crawler import crawl
from internal_link_graph.graph import LinkGraph
from internal_link_graph.models import Link

HOME = f"{BASE}/"


def u(name: str) -> str:
    return f"{BASE}/{name}"


def codes(result, url=None):
    return [issue.code for issue in result.issues if url is None or issue.url == url]


def test_node_metrics_on_small_site(small_site):
    result = analyze(small_site)
    assert result.start_url == HOME
    home, a, b, c, o = (result.nodes[x] for x in (HOME, u("a"), u("b"), u("c"), u("o")))
    assert (home.in_degree, home.out_degree, home.depth) == (1, 2, 0)
    assert (a.in_degree, a.out_degree, a.depth) == (3, 2, 1)
    assert (b.in_degree, b.depth) == (2, 1)
    assert (c.in_degree, c.depth) == (1, 2)
    assert (o.in_degree, o.depth) == (0, None)
    assert sum(node.pagerank for node in result.nodes.values()) == pytest.approx(1.0)
    assert a.pagerank == max(node.pagerank for node in result.nodes.values())
    assert o.pagerank == min(node.pagerank for node in result.nodes.values())
    assert home.component_size == 4 and o.component_size == 1
    assert home.personalized_pagerank is None


def test_structure_lists_on_small_site(small_site):
    result = analyze(small_site)
    assert result.orphans == [u("o")]
    assert result.unreachable == [] and result.dead_ends == []
    assert result.components[0] == [HOME, u("a"), u("b"), u("c")]
    assert result.depth_histogram() == {0: 1, 1: 2, 2: 1}
    assert [node.url for node in result.top_pages(2)] == [u("a"), u("b")]
    assert result.top_pages(1, key="in_degree")[0].url == u("a")


def test_summary_values(small_site):
    summary = analyze(small_site).summary
    assert summary["pages"] == 5 and summary["edges"] == 7
    assert summary["internal_links"] == 7 and summary["external_links"] == 0
    assert summary["orphans"] == 1 and summary["reachable_pages"] == 4
    assert summary["max_depth"] == 2 and summary["average_depth"] == pytest.approx(1.0)
    assert summary["average_in_degree"] == pytest.approx(1.4)
    assert summary["largest_component"] == 4 and summary["components"] == 2
    assert summary["status_codes"] == {"2xx": 5}
    assert summary["pagerank_converged"] is True
    assert summary["issues"] == sum(summary["issues_by_severity"].values())


def test_personalized_pagerank_option(small_site):
    result = analyze(small_site, AnalysisConfig(personalized=True))
    assert result.nodes[u("o")].personalized_pagerank == pytest.approx(0.0, abs=1e-12)
    assert result.nodes[HOME].personalized_pagerank > result.nodes[HOME].pagerank


def test_orphan_and_unreachable_are_distinguished():
    site = build_site([("home", "a"), ("x", "y"), ("y", "x")], start="home")
    result = analyze(site)
    assert result.orphans == []
    assert result.unreachable == [u("x"), u("y")]
    assert codes(result).count("unreachable_page") == 2
    site = build_site([("home", "a"), ("x", "y")], start="home")
    result = analyze(site)
    assert result.orphans == [u("x")] and result.unreachable == [u("y")]


def test_sitemap_only_url_is_an_orphan_with_detail():
    site = build_site([("home", "a"), ("a", "home")], start="home")
    site.sitemap_urls.add(u("forgotten"))
    result = analyze(site)
    issue = next(issue for issue in result.issues if issue.code == "orphan_page")
    assert issue.url == u("forgotten") and "sitemap" in issue.detail
    assert result.nodes[u("forgotten")].in_sitemap


def test_page_only_linked_with_nofollow_is_an_orphan():
    site = build_site([("home", "a"), ("a", "home"), ("home", "nf")], start="home")
    site.links[2].nofollow = True
    site.links[2].rel = ("nofollow",)
    result = analyze(site)
    assert u("nf") in result.orphans
    orphan = next(issue for issue in result.issues if issue.code == "orphan_page")
    assert "nofollow" in orphan.detail
    assert "nofollow_internal" in codes(result, HOME)
    assert result.nodes[u("nf")].nofollow_inlinks == 1
    assert result.summary["equity_lost_by_kind"]["nofollow"] > 0
    included = analyze(site, AnalysisConfig(follow_only=False))
    assert included.orphans == []


def test_broken_and_redirect_links():
    site = build_site(
        [("home", "gone"), ("home", "old"), ("home", "new"), ("new", "home")],
        start="home",
        gone={"status": 404, "crawled": True},
        old={"status": 301, "redirect_target": u("new"), "crawled": True,
             "redirect_chain": [u("old"), u("new")]},
    )
    result = analyze(site)
    assert u("gone") not in result.nodes and u("old") not in result.nodes
    broken = next(issue for issue in result.issues if issue.code == "broken_link")
    assert (broken.url, broken.target, broken.severity) == (HOME, u("gone"), "error")
    assert "404" in broken.detail
    redirect = next(issue for issue in result.issues if issue.code == "redirect_link")
    assert redirect.target == u("old") and u("new") in redirect.detail
    assert "redirect_chain" not in codes(result)
    assert result.nodes[u("new")].inlinks == 2  # direct link plus the redirected one
    assert result.equity.counts == {"broken": 1, "redirect": 1, "nofollow": 0}


def test_redirect_chain_issue_needs_more_than_one_hop():
    site = build_site(
        [("home", "r1")],
        start="home",
        r1={"status": 301, "redirect_target": u("r2"),
            "redirect_chain": [u("r1"), u("r2"), u("final")]},
        r2={"status": 301, "redirect_target": u("final")},
    )
    site.page(u("final")).status = 200
    result = analyze(site)
    chain = next(issue for issue in result.issues if issue.code == "redirect_chain")
    assert chain.url == u("r1") and chain.detail.startswith("2 hops")
    assert result.graph.has_edge(HOME, u("final"))


def test_link_issues_are_reported_once_per_pair():
    site = build_site([("home", "gone"), ("home", "gone"), ("home", "gone")], start="home",
                      gone={"status": 500})
    assert codes(analyze(site)).count("broken_link") == 1


def test_anchor_issues():
    site = build_site([("home", "a"), ("home", "b"), ("home", "c"), ("a", "home")], start="home")
    site.links[0].anchor = "Click here"
    site.links[1].anchor = "  "
    site.links[2].anchor = ""
    site.links[2].is_image = True
    result = analyze(site)
    by_target = {issue.target: issue for issue in result.issues if issue.url == HOME}
    assert by_target[u("a")].code == "generic_anchor"
    assert by_target[u("b")].detail == "no anchor text"
    assert by_target[u("c")].detail == "image without alt text"


def test_over_optimized_anchor_issue():
    edges = [(f"s{i}", "money") for i in range(6)] + [("home", f"s{i}") for i in range(6)]
    site = build_site(edges, start="home")
    for link in site.links[:6]:
        link.anchor = "cheap garden shears"
    result = analyze(site)
    issue = next(issue for issue in result.issues if issue.code == "over_optimized_anchor")
    assert issue.url == u("money") and "100%" in issue.detail


def test_deep_pages_and_threshold():
    chain = [("home", "l1"), ("l1", "l2"), ("l2", "l3"), ("l3", "l4"), ("l4", "l5")]
    site = build_site(chain, start="home")
    result = analyze(site)
    deep = [issue.url for issue in result.issues if issue.code == "deep_page"]
    assert deep == [u("l4"), u("l5")]
    assert result.dead_ends == [u("l5")]
    shallow = analyze(site, AnalysisConfig(deep_threshold=2))
    assert codes(shallow).count("deep_page") == 4


def test_excessive_outlinks_counts_internal_and_external():
    site = build_site([("home", f"p{i}") for i in range(4)], start="home")
    site.add_link(Link(HOME, "https://other.example.org/", internal=False))
    result = analyze(site, AnalysisConfig(max_outlinks=4))
    issue = next(issue for issue in result.issues if issue.code == "excessive_outlinks")
    assert issue.url == HOME and "5 links (4 internal, 1 external)" in issue.detail
    assert "excessive_outlinks" not in codes(analyze(site, AnalysisConfig(max_outlinks=5)))


def test_low_inlinks_threshold(small_site):
    result = analyze(small_site)
    assert [i.url for i in result.issues if i.code == "low_inlinks"] == [u("c")]
    relaxed = analyze(small_site, AnalysisConfig(low_inlinks=0))
    assert "low_inlinks" not in codes(relaxed)


def test_noindex_canonical_title_and_h1_issues(small_site):
    small_site.pages[u("a")].meta_robots = "noindex"
    small_site.pages[u("b")].canonical = u("b-canonical")
    small_site.pages[u("c")].title = ""
    small_site.pages[u("c")].h1 = ""
    result = analyze(small_site)
    assert "linked_noindex" in codes(result, u("a"))
    canonical = next(i for i in result.issues if i.code == "linked_canonicalized")
    assert canonical.url == u("b") and canonical.target == u("b-canonical")
    assert {"missing_title", "missing_h1"} <= set(codes(result, u("c")))
    assert not result.nodes[u("a")].indexable
    assert result.summary["indexable_pages"] == 3


def test_content_issues_are_skipped_for_uncrawled_pages():
    result = analyze(build_site([("home", "a"), ("a", "home")], start="home"))
    assert "missing_title" not in codes(result) and "missing_h1" not in codes(result)


def test_duplicate_title_issue(small_site):
    small_site.pages[u("a")].title = "Same title for two pages"
    small_site.pages[u("b")].title = "Same title for two pages"
    result = analyze(small_site)
    dupes = [issue for issue in result.issues if issue.code == "duplicate_title"]
    assert [issue.url for issue in dupes] == [u("a"), u("b")]
    assert "identical" in dupes[0].detail
    assert result.summary["duplicate_title_clusters"] == 1


def test_blocked_by_robots_pages():
    site = build_site(
        [("home", "private"), ("home", "a"), ("a", "home")],
        start="home",
        private={"blocked_by_robots": True, "crawled": True, "error": "blocked by robots.txt"},
    )
    result = analyze(site)
    blocked = next(issue for issue in result.issues if issue.code == "blocked_by_robots")
    assert blocked.target == u("private") and blocked.severity == "notice"
    assert u("private") not in result.dead_ends
    assert result.summary["status_codes"].get("blocked") == 1


def test_dead_ends_after_a_crawl_only_include_fetched_pages(fetcher):
    site = crawl(HOME, fetcher=fetcher, delay=0, workers=1, max_pages=3, use_sitemap=False)
    result = analyze(site)
    assert all(site.pages[url].crawled for url in result.dead_ends)
    assert result.summary["status_codes"].get("not_crawled", 0) > 0


def test_analysis_of_crawled_fixture_site(fetcher):
    result = analyze(crawl(HOME, fetcher=fetcher, delay=0, workers=1))
    counts = result.issue_counts()
    assert counts["broken_link"] == 1 and counts["redirect_link"] == 1
    assert counts["orphan_page"] == 1 and counts["generic_anchor"] == 1
    assert result.dead_ends == [u("blog/rose-diseases")]
    assert result.summary["external_links"] == 1
    assert result.nodes[u("blog/pruning-roses")].cluster == "/blog/"
    assert result.nodes[u("blog/rose-diseases")].external_outlinks == 1


def test_issues_are_sorted_by_severity_then_code(fetcher):
    result = analyze(crawl(HOME, fetcher=fetcher, delay=0, workers=1))
    ranks = [SEVERITIES.index(issue.severity) for issue in result.issues]
    assert ranks == sorted(ranks)
    assert list(result.issue_counts()) == [
        code for code in ISSUE_TYPES if code in result.issue_counts()
    ]
    assert set(result.issues_by_code()) == set(result.issue_counts())


def test_issue_metadata():
    for code, (severity, title) in ISSUE_TYPES.items():
        assert severity in SEVERITIES and title
        assert Issue(code, "u").to_dict()["severity"] == severity
    unknown = Issue("made_up", "u")
    assert unknown.severity == "notice" and unknown.title == "made_up"


def test_to_dict_is_json_serialisable_and_truncatable(small_site):
    result = analyze(small_site)
    data = json.loads(json.dumps(result.to_dict()))
    assert data["format"] == "internal-link-graph/analysis"
    assert len(data["nodes"]) == 5 and data["nodes"][0]["url"] == u("a")
    assert data["depth_histogram"] == {"0": 1, "1": 2, "2": 1}
    assert data["components"] == [[HOME, u("a"), u("b"), u("c")]]
    assert len(result.to_dict(max_items=2)["nodes"]) == 2


def test_choose_start_url_fallbacks():
    site = build_site([("deep/page", "home"), ("home", "x"), ("home", "y")])
    graph = LinkGraph.from_site(site)
    assert choose_start_url(site, graph, preferred=u("x")) == u("x")
    assert choose_start_url(site, graph, preferred=u("unknown")) == HOME
    site.start_url = u("deep/page")
    assert choose_start_url(site, graph) == u("deep/page")
    no_root = build_site([("hub", "p1"), ("hub", "p2"), ("p1", "hub")])
    assert choose_start_url(no_root, LinkGraph.from_site(no_root)) in {u("hub"), u("p1"), u("p2")}
    assert choose_start_url(build_site([]), LinkGraph()) == ""


def test_start_url_that_redirects_is_resolved():
    site = build_site(
        [("www", "a"), ("a", "www")], start="entry",
        entry={"status": 301, "redirect_target": u("www")},
    )
    assert analyze(site).start_url == u("www")


def test_empty_site_does_not_crash():
    result = analyze(build_site([]))
    assert result.summary["pages"] == 0 and result.issues == []
    assert result.summary["average_in_degree"] == 0.0


def test_analysis_is_deterministic(small_site):
    first = analyze(small_site).to_dict()
    second = analyze(small_site).to_dict()
    assert first == second
