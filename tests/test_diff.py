import json

import pytest

from conftest import BASE, build_site
from internal_link_graph.analyzer import AnalysisConfig, analyze
from internal_link_graph.diff import CrawlDiff, Mover, diff_analyses, diff_sites, format_diff

HOME = f"{BASE}/"
OLD_EDGES = [("home", "a"), ("home", "b"), ("a", "home"), ("b", "home"), ("b", "c"), ("c", "home")]
NEW_EDGES = [("home", "a"), ("home", "b"), ("a", "home"), ("b", "home"), ("home", "c"),
             ("c", "home"), ("c", "d"), ("d", "home")]


def u(name: str) -> str:
    return f"{BASE}/{name}"


@pytest.fixture
def diff() -> CrawlDiff:
    old = build_site(OLD_EDGES, start="home")
    new = build_site(NEW_EDGES, start="home")
    return diff_sites(old, new)


def test_identical_crawls_have_no_changes():
    site = build_site(OLD_EDGES, start="home")
    same = diff_sites(site, build_site(OLD_EDGES, start="home"))
    assert not same.has_changes
    assert same.gainers == [] and same.losers == [] and same.issue_changes == {}
    text = format_diff(same)
    assert "No structural changes." in text and "Pages: 4 -> 4 (+0 / -0)" in text


def test_new_and_lost_pages_and_links(diff):
    assert diff.new_pages == [u("d")] and diff.lost_pages == []
    assert diff.new_links == [(HOME, u("c")), (u("c"), u("d")), (u("d"), HOME)]
    assert diff.lost_links == [(u("b"), u("c"))]
    assert diff.has_changes
    reverse = diff_sites(build_site(NEW_EDGES, start="home"), build_site(OLD_EDGES, start="home"))
    assert reverse.lost_pages == [u("d")] and reverse.new_links == [(u("b"), u("c"))]


def test_depth_changes(diff):
    assert diff.depth_changes == [(u("c"), 2, 1)]


def test_pagerank_movers(diff):
    gain = {mover.url: mover for mover in diff.gainers}
    assert u("c") in gain and gain[u("c")].delta > 0 and gain[u("c")].relative > 0
    assert all(mover.delta < 0 for mover in diff.losers)
    assert u("d") not in gain  # new pages have no previous score to compare with
    deltas = [mover.delta for mover in diff.gainers]
    assert deltas == sorted(deltas, reverse=True)
    assert len(diff_sites(build_site(OLD_EDGES, start="home"),
                          build_site(NEW_EDGES, start="home"), top=1).losers) == 1


def test_mover_properties():
    mover = Mover("u", 0.2, 0.25)
    assert mover.delta == pytest.approx(0.05) and mover.relative == pytest.approx(0.25)
    assert Mover("u", 0.0, 0.1).relative == 0.0
    assert mover.to_dict()["relative"] == pytest.approx(0.25)


def test_status_changes_ignore_unknown_statuses():
    old = build_site(OLD_EDGES, start="home", a={"status": 200}, b={"status": 200})
    new = build_site(OLD_EDGES, start="home", a={"status": 404}, b={"status": 0})
    result = diff_sites(old, new)
    assert result.status_changes == [(u("a"), 200, 404)]
    assert result.lost_pages == [u("a")]
    assert result.issue_changes["broken_link"] == (0, 1)


def test_orphan_changes():
    old = build_site([("home", "a"), ("a", "home"), ("x", "home")], start="home")
    new = build_site([("home", "a"), ("a", "home"), ("a", "x"), ("x", "home"), ("y", "home")],
                     start="home")
    result = diff_sites(old, new)
    assert result.fixed_orphans == [u("x")] and result.new_orphans == [u("y")]
    assert (u("x"), None, 2) in result.depth_changes


def test_removed_orphan_is_not_reported_as_fixed():
    old = build_site([("home", "a"), ("a", "home"), ("x", "home")], start="home")
    new = build_site([("home", "a"), ("a", "home")], start="home")
    result = diff_sites(old, new)
    assert result.fixed_orphans == [] and result.lost_pages == [u("x")]
    assert result.issue_changes["orphan_page"] == (1, 0)


def test_summary_counts(diff):
    assert diff.summary["old_pages"] == 4 and diff.summary["new_pages"] == 5
    assert diff.summary["pages_added"] == 1 and diff.summary["pages_removed"] == 0
    assert diff.summary["old_edges"] == 6 and diff.summary["new_edges"] == 8
    assert diff.summary["links_added"] == 3 and diff.summary["links_removed"] == 1
    assert diff.summary["depth_changes"] == 1


def test_to_dict_is_json_serialisable(diff):
    data = json.loads(json.dumps(diff.to_dict()))
    assert data["format"] == "internal-link-graph/diff"
    assert data["new_links"][0] == {"source": HOME, "target": u("c")}
    assert data["depth_changes"] == [{"url": u("c"), "old": 2, "new": 1}]
    assert data["pagerank_gainers"][0]["delta"] > 0


def test_format_diff_sections(diff):
    text = format_diff(diff)
    assert text.startswith("Crawl comparison\n")
    assert "Pages: 4 -> 5 (+1 / -0)" in text and "Links: 6 -> 8 (+3 / -1)" in text
    assert "New pages (1)" in text and f"  {u('d')}" in text
    assert f"{u('b')} -> {u('c')}" in text
    assert "Click depth changes (1)" in text and f"{u('c')}  2 -> 1" in text
    assert "Lost pages" not in text and "No structural changes." not in text


def test_format_diff_truncates_long_sections():
    old = build_site([("home", "a"), ("a", "home")], start="home")
    edges = [("home", "a"), ("a", "home")] + [("home", f"n{i}") for i in range(6)]
    text = format_diff(diff_sites(old, build_site(edges, start="home")), limit=2)
    assert "New pages (6)" in text and "... and 4 more" in text


def test_unreachable_depth_is_spelled_out():
    old = build_site([("home", "a"), ("a", "home"), ("a", "x"), ("x", "a")], start="home")
    new = build_site([("home", "a"), ("a", "home"), ("x", "a"), ("y", "x")], start="home")
    text = format_diff(diff_sites(old, new))
    assert f"{u('x')}  2 -> unreachable" in text


def test_diff_analyses_and_config_passthrough():
    old = build_site(OLD_EDGES, start="home")
    new = build_site(NEW_EDGES, start="home")
    direct = diff_analyses(analyze(old), analyze(new))
    assert direct.to_dict() == diff_sites(old, new).to_dict()
    new.links[4].nofollow = True  # home -> c
    assert (HOME, u("c")) not in diff_sites(old, new).new_links
    included = diff_sites(old, new, AnalysisConfig(follow_only=False))
    assert (HOME, u("c")) in included.new_links
