import pytest

from conftest import BASE, build_site
from internal_link_graph.anchors import (
    anchor_profiles,
    is_generic_anchor,
    normalize_anchor,
    over_optimized,
)
from internal_link_graph.clusters import (
    cluster_sizes,
    duplicate_title_clusters,
    jaccard,
    normalize_title,
    strip_site_suffix,
    title_shingles,
    topic_clusters,
)
from internal_link_graph.equity import link_equity_loss
from internal_link_graph.graph import LinkGraph
from internal_link_graph.models import Link


def u(name: str) -> str:
    return f"{BASE}/{name}"


# -- equity ---------------------------------------------------------------------


def test_equity_loss_shares_are_split_per_distinct_target():
    site = build_site(
        [("home", "ok"), ("home", "gone"), ("home", "old"), ("home", "nf")],
        start="home",
        gone={"status": 404},
        old={"status": 301, "redirect_target": u("ok")},
    )
    site.links[3].nofollow = True
    report = link_equity_loss(site, {f"{BASE}/": 0.4}, damping=0.85)
    share = 0.85 * 0.4 / 4
    assert report.counts == {"broken": 1, "redirect": 1, "nofollow": 1}
    assert report.totals["broken"] == pytest.approx(share)
    assert report.total == pytest.approx(3 * share)
    assert report.by_source[f"{BASE}/"] == pytest.approx(3 * share)
    kinds = {loss.target: loss.kind for loss in report.losses}
    assert kinds == {u("gone"): "broken", u("old"): "redirect", u("nf"): "nofollow"}


def test_equity_redirect_records_final_target_and_status():
    site = build_site([("home", "old")], old={"status": 302, "redirect_target": u("new")})
    loss = link_equity_loss(site, {f"{BASE}/": 1.0}).losses[0]
    assert (loss.kind, loss.status, loss.final_target) == ("redirect", 302, u("new"))
    assert loss.to_dict()["amount"] == pytest.approx(0.85)


def test_equity_redirect_to_broken_page_counts_as_broken():
    site = build_site(
        [("home", "old")],
        old={"status": 301, "redirect_target": u("gone")},
        gone={"status": 410},
    )
    site.page(u("gone")).status = 410
    assert link_equity_loss(site, {f"{BASE}/": 1.0}).losses[0].kind == "broken"


def test_equity_parallel_links_are_nofollow_only_if_all_are():
    site = build_site([("home", "a"), ("home", "a")])
    site.links[0].nofollow = True
    assert link_equity_loss(site, {f"{BASE}/": 1.0}).losses == []
    site.links[1].nofollow = True
    assert link_equity_loss(site, {f"{BASE}/": 1.0}).counts["nofollow"] == 1


def test_equity_ignores_external_self_and_unranked_sources():
    site = build_site([("home", "gone"), ("a", "gone")], gone={"status": 404})
    site.add_link(Link(f"{BASE}/", "https://other.example.org/", internal=False))
    site.add_link(Link(f"{BASE}/", f"{BASE}/"))
    report = link_equity_loss(site, {f"{BASE}/": 0.5})
    assert len(report.losses) == 1  # /a has no rank, external and self links are skipped
    assert report.losses[0].amount == pytest.approx(0.85 * 0.5)


def test_equity_losses_sorted_and_serialisable():
    site = build_site([("home", "gone"), ("a", "gone"), ("a", "x")], gone={"status": 500})
    report = link_equity_loss(site, {f"{BASE}/": 0.2, u("a"): 0.6})
    assert [loss.source for loss in report.losses] == [u("a"), f"{BASE}/"]
    assert report.top(1)[0].source == u("a")
    assert report.top(kind="redirect") == []
    data = report.to_dict(limit=1)
    assert len(data["losses"]) == 1 and data["counts"]["broken"] == 2


# -- anchors --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  Read   More » ", "read more"), ("CLICK HERE!", "click here"), ("« Back", "back"),
     ("Straße", "strasse"), ("", "")],
)
def test_normalize_anchor(raw, expected):
    assert normalize_anchor(raw) == expected


@pytest.mark.parametrize("text", ["Click here", "read more...", "HERE", "Learn more →",
                                  "আরও পড়ুন"])
def test_generic_anchors_are_detected(text):
    assert is_generic_anchor(text)


def test_descriptive_and_empty_anchors_are_not_generic():
    assert not is_generic_anchor("Rose pruning guide")
    assert not is_generic_anchor("")
    assert is_generic_anchor("Buy now", extra=["buy now"])


def make_links(target, anchors):
    return [Link(u(f"s{i}"), u(target), anchor=text) for i, text in enumerate(anchors)]


def test_anchor_profile_counts():
    links = make_links("t", ["Roses", "roses", "Rose guide", "click here", ""])
    links.append(Link(u("img"), u("t"), anchor="", is_image=True))
    links.append(Link(u("x"), "https://other.example.org/", anchor="ext", internal=False))
    profile = anchor_profiles(links)[u("t")]
    assert profile.total == 6 and profile.empty == 2 and profile.image == 1
    assert profile.generic == 1 and profile.unique == 3
    assert profile.top_anchor == "roses"
    assert profile.top_share == pytest.approx(2 / 4)
    assert profile.diversity == pytest.approx(3 / 4)
    assert profile.distribution(limit=1) == [("roses", 2, 0.5)]
    assert len(profile.sources) == 6
    assert profile.to_dict()["top_anchor"] == "roses"


def test_anchor_profiles_resolve_redirects_and_skip_self_links():
    links = [Link(u("a"), u("old"), anchor="Old"), Link(u("b"), u("b"), anchor="Self")]
    profiles = anchor_profiles(links, resolve={u("old"): u("new")})
    assert set(profiles) == {u("new")}


def test_empty_profile_properties():
    profile = anchor_profiles([Link(u("a"), u("t"), anchor="")])[u("t")]
    assert profile.top_anchor == "" and profile.top_share == 0.0 and profile.diversity == 0.0


def test_over_optimized_detection():
    profiles = anchor_profiles(
        make_links("money", ["best rose shears"] * 9 + ["shears"])
        + make_links("varied", ["a", "b", "c", "d", "e", "f"])
        + make_links("generic", ["read more"] * 8)
        + make_links("few", ["exact match"] * 3)
    )
    flagged = over_optimized(profiles)
    assert [profile.target for profile in flagged] == [u("money")]
    assert over_optimized(profiles, min_links=3)[0].target == u("few")
    assert over_optimized(profiles, dominance=0.95) == []


def test_over_optimized_needs_several_sources():
    links = [Link(u("one"), u("t"), anchor="same anchor") for _ in range(10)]
    assert over_optimized(anchor_profiles(links)) == []


# -- topic clusters ---------------------------------------------------------------


def cluster_graph():
    return LinkGraph.from_edges(
        [
            (f"{BASE}/", u("blog/")),
            (u("blog/"), u("blog/a")),
            (u("blog/"), u("blog/b")),
            (u("blog/a"), u("blog/b")),
            (u("blog/a"), u("shop/x")),
            (u("shop/x"), u("shop/y")),
            (u("about"), f"{BASE}/"),
        ],
        nodes=[u("docs/lonely")],
    )


def test_topic_cluster_assignment_and_hub_pages():
    report = topic_clusters(cluster_graph())
    assert report.assignment[u("blog/a")] == "/blog/"
    assert report.assignment[u("blog/")] == "/blog/"  # hub page joins its own directory
    assert report.assignment[f"{BASE}/"] == "/"
    assert report.assignment[u("about")] == "/"
    assert report.names[0] == "/blog/"
    assert sorted(report.members["/shop/"]) == [u("shop/x"), u("shop/y")]


def test_topic_cluster_link_matrix_and_cohesion():
    report = topic_clusters(cluster_graph())
    assert report.internal_links("/blog/") == 3
    assert report.links_between("/blog/", "/shop/") == 1
    assert report.outgoing_links("/blog/") == 1
    assert report.incoming_links("/blog/") == 1
    assert report.cohesion("/blog/") == pytest.approx(3 / 4)
    assert report.cohesion("/docs/") == 0.0
    assert report.isolated() == ["/docs/"]


def test_topic_cluster_depth_two_and_serialisation():
    graph = LinkGraph.from_edges(
        [(u("blog/2026/a"), u("blog/2026/b")), (u("blog/2025/c"), u("blog/2026/a"))]
    )
    report = topic_clusters(graph, depth=2)
    assert set(report.members) == {"/blog/2026/", "/blog/2025/"}
    data = report.to_dict()
    assert data["depth"] == 2
    assert {item["name"] for item in data["clusters"]} == {"/blog/2026/", "/blog/2025/"}


# -- duplicate titles -------------------------------------------------------------


def test_title_normalisation_and_shingles():
    assert normalize_title("  Rose-Pruning:  A GUIDE! ") == "rose pruning a guide"
    assert title_shingles("") == frozenset()
    assert title_shingles("ab") == frozenset({"ab"})
    assert title_shingles("abcd") == frozenset({"abc", "bcd"})


def test_jaccard():
    assert jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)
    assert jaccard(set(), {"a"}) == 0.0
    assert jaccard({"a"}, {"b"}) == 0.0
    assert jaccard({"a"}, {"a"}) == 1.0


def test_exact_duplicate_titles_cluster():
    clusters = duplicate_title_clusters(
        {u("a"): "Garden Tools", u("b"): "garden tools!", u("c"): "Compost bins", u("d"): ""}
    )
    assert len(clusters) == 1
    assert clusters[0].urls == [u("a"), u("b")] and clusters[0].exact
    assert clusters[0].titles == ["Garden Tools", "garden tools!"]
    assert cluster_sizes(clusters) == 2


def test_near_duplicate_titles_cluster_with_threshold():
    titles = {
        u("p1"): "Best garden shears for pruning roses - page 1",
        u("p2"): "Best garden shears for pruning roses - page 2",
        u("x"): "How to build a compost bin from pallets",
    }
    clusters = duplicate_title_clusters(titles, strip_brand=False)
    assert len(clusters) == 1 and not clusters[0].exact
    assert clusters[0].urls == [u("p1"), u("p2")]
    assert duplicate_title_clusters(titles, threshold=1.0, strip_brand=False) == []


def test_brand_suffix_does_not_make_everything_a_duplicate():
    titles = {
        u("a"): "Roses | Example Garden Supplies Store",
        u("b"): "Tulips | Example Garden Supplies Store",
        u("c"): "Compost | Example Garden Supplies Store",
        u("d"): "Roses | Example Garden Supplies Store",
    }
    assert duplicate_title_clusters(titles, threshold=0.5, strip_brand=False)[0].size == 4
    clusters = duplicate_title_clusters(titles)
    assert [cluster.urls for cluster in clusters] == [[u("a"), u("d")]]
    assert clusters[0].to_dict()["exact"] is True


def test_strip_site_suffix_and_prefix():
    stripped = strip_site_suffix(
        {"1": "Brand - Roses", "2": "Brand - Tulips", "3": "Brand - Compost", "4": "Unrelated"}
    )
    assert stripped == {"1": "Roses", "2": "Tulips", "3": "Compost", "4": "Unrelated"}
    few = {"1": "A | B", "2": "C | B"}
    assert strip_site_suffix(few) == few


def test_bangla_duplicate_titles():
    clusters = duplicate_title_clusters({u("a"): "গোলাপ গাছের যত্ন", u("b"): "গোলাপ গাছের যত্ন",
                                         u("c"): "সার তৈরির নিয়ম"})
    assert len(clusters) == 1 and clusters[0].size == 2
