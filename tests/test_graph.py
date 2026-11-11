import math

import pytest

from conftest import BASE, build_site
from internal_link_graph.graph import (
    LinkGraph,
    click_depth,
    dead_ends,
    hits,
    pagerank,
    shortest_path,
    sources,
    strongly_connected_components,
)
from internal_link_graph.models import Link


def g(*edges, nodes=()):
    return LinkGraph.from_edges(edges, nodes=nodes)


def test_graph_basics():
    graph = g(("a", "b"), ("a", "b"), ("b", "c"), ("a", "a"), nodes=["z"])
    assert len(graph) == 4 and "z" in graph and "q" not in graph
    assert graph.edge_count == 2 and graph.link_count == 3
    assert graph.has_edge("a", "b") and not graph.has_edge("b", "a")
    assert not graph.has_edge("a", "a")  # self loops are dropped
    assert graph.successors("a") == ["b"] and graph.predecessors("c") == ["b"]
    assert graph.out_degree("a") == 1 and graph.in_degree("b") == 1
    assert graph.out_degree("unknown") == 0 and graph.successors("unknown") == []
    assert ("a", "b", 2) in graph.edges()


def test_self_loops_can_be_kept():
    graph = LinkGraph.from_edges([("a", "a")], allow_self_loops=True)
    assert graph.has_edge("a", "a")


def test_density_and_reciprocity():
    graph = g(("a", "b"), ("b", "a"), ("a", "c"))
    assert graph.density() == pytest.approx(3 / 6)
    assert graph.reciprocity() == pytest.approx(2 / 3)
    assert LinkGraph().density() == 0.0 and LinkGraph().reciprocity() == 0.0


def test_pagerank_empty_and_single_node():
    assert pagerank(LinkGraph()).scores == {}
    assert pagerank(g(nodes=["a"])).scores == {"a": pytest.approx(1.0)}


def test_pagerank_two_node_cycle_is_uniform():
    scores = pagerank(g(("a", "b"), ("b", "a"))).scores
    assert scores["a"] == pytest.approx(0.5) and scores["b"] == pytest.approx(0.5)


def test_pagerank_dangling_node_hand_computed():
    # a -> b, b dangling, d = 0.85:
    #   pa = 0.075 + 0.425 * pb,  pb = 0.075 + 0.85 * pa + 0.425 * pb
    #   => pa = 20/57, pb = 37/57
    scores = pagerank(g(("a", "b"))).scores
    assert scores["a"] == pytest.approx(20 / 57, abs=1e-8)
    assert scores["b"] == pytest.approx(37 / 57, abs=1e-8)


def test_pagerank_three_node_hand_computed():
    # a -> b, a -> c, b -> c, c -> a with d = 0.85 and N = 3:
    #   pb = 0.05 + 0.425 pa
    #   pc = 0.05 + 0.425 pa + 0.85 pb = 0.0925 + 0.78625 pa
    #   pa + pb + pc = 1  =>  pa = 0.8575 / 2.21125
    pa = 0.8575 / 2.21125
    scores = pagerank(g(("a", "b"), ("a", "c"), ("b", "c"), ("c", "a"))).scores
    assert scores["a"] == pytest.approx(pa, abs=1e-8)
    assert scores["b"] == pytest.approx(0.05 + 0.425 * pa, abs=1e-8)
    assert scores["c"] == pytest.approx(0.0925 + 0.78625 * pa, abs=1e-8)
    assert scores["a"] == pytest.approx(0.387790, abs=1e-6)


def test_pagerank_star_hand_computed():
    # hub <-> three leaves: ph = 0.0375 + 0.85 (1 - ph)  =>  ph = 0.8875 / 1.85
    edges = [("hub", leaf) for leaf in "xyz"] + [(leaf, "hub") for leaf in "xyz"]
    scores = pagerank(g(*edges)).scores
    hub = 0.8875 / 1.85
    assert scores["hub"] == pytest.approx(hub, abs=1e-8)
    for leaf in "xyz":
        assert scores[leaf] == pytest.approx((1 - hub) / 3, abs=1e-8)


def test_pagerank_isolated_nodes_share_rank_evenly():
    scores = pagerank(g(nodes=["a", "b", "c", "d"])).scores
    assert all(value == pytest.approx(0.25) for value in scores.values())


@pytest.mark.parametrize("damping", [0.0, 0.5, 0.85, 1.0])
def test_pagerank_sums_to_one(damping):
    graph = g(("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("e", "a"))
    result = pagerank(graph, damping=damping)
    assert sum(result.scores.values()) == pytest.approx(1.0)
    assert all(value >= 0 for value in result.scores.values())


def test_pagerank_zero_damping_is_uniform():
    scores = pagerank(g(("a", "b"), ("a", "c")), damping=0.0).scores
    assert all(value == pytest.approx(1 / 3) for value in scores.values())


def test_pagerank_rejects_bad_damping():
    with pytest.raises(ValueError):
        pagerank(g(("a", "b")), damping=1.5)


def test_pagerank_reports_convergence():
    graph = g(("a", "b"), ("b", "c"), ("c", "a"), ("a", "c"))
    done = pagerank(graph)
    assert done.converged and done.iterations > 1 and done.error < 1e-10
    cut = pagerank(graph, max_iterations=2)
    assert not cut.converged and cut.iterations == 2


def test_pagerank_parallel_links_only_count_when_weighted():
    # a -> b (x2), a -> c, b -> a, c -> a
    graph = g(("a", "b"), ("a", "b"), ("a", "c"), ("b", "a"), ("c", "a"))
    plain = pagerank(graph).scores
    assert plain["b"] == pytest.approx(plain["c"])
    weighted = pagerank(graph, weighted=True).scores
    pa = 0.9 / 1.85
    assert weighted["a"] == pytest.approx(pa, abs=1e-8)
    assert weighted["b"] == pytest.approx(0.05 + 0.85 * pa * 2 / 3, abs=1e-8)
    assert weighted["c"] == pytest.approx(0.05 + 0.85 * pa / 3, abs=1e-8)


def test_personalized_pagerank_hand_computed():
    # a <-> b, teleport only to a: pa = 0.15 + 0.85 pb, pb = 0.85 pa => pa = 1 / 1.85
    scores = pagerank(g(("a", "b"), ("b", "a")), personalization={"a": 1.0}).scores
    assert scores["a"] == pytest.approx(1 / 1.85, abs=1e-8)
    assert scores["b"] == pytest.approx(0.85 / 1.85, abs=1e-8)


def test_personalized_pagerank_unreachable_nodes_get_nothing():
    graph = g(("a", "b"), ("b", "a"), ("x", "a"))
    scores = pagerank(graph, personalization={"a": 2.0, "unknown": 5.0}).scores
    assert scores["x"] == pytest.approx(0.0, abs=1e-12)
    with pytest.raises(ValueError):
        pagerank(graph, personalization={"unknown": 1.0})


def test_pagerank_top_is_sorted_with_stable_ties():
    result = pagerank(g(("a", "b"), ("b", "a"), ("c", "a")))
    assert [url for url, _ in result.top(2)] == ["a", "b"]
    uniform = pagerank(g(nodes=["b", "a"]))
    assert [url for url, _ in uniform.top()] == ["a", "b"]


def test_hits_hand_computed():
    result = hits(g(("a", "b"), ("a", "c")))
    assert result.hubs["a"] == pytest.approx(1.0)
    assert result.hubs["b"] == pytest.approx(0.0)
    assert result.authorities["b"] == pytest.approx(1 / math.sqrt(2))
    assert result.authorities["c"] == pytest.approx(1 / math.sqrt(2))
    assert result.authorities["a"] == pytest.approx(0.0)
    assert result.converged


def test_hits_prefers_well_linked_authority():
    result = hits(g(("h1", "x"), ("h2", "x"), ("h3", "x"), ("h1", "y")))
    assert result.authorities["x"] > result.authorities["y"]
    assert result.hubs["h1"] > result.hubs["h2"]
    norm = math.sqrt(sum(value**2 for value in result.authorities.values()))
    assert norm == pytest.approx(1.0)


def test_hits_empty_and_edgeless_graphs():
    assert hits(LinkGraph()).hubs == {}
    result = hits(g(nodes=["a", "b"]))
    assert set(result.authorities) == {"a", "b"}
    assert all(value == 0 for value in result.authorities.values())


def test_click_depth_bfs():
    graph = g(("s", "a"), ("s", "b"), ("a", "c"), ("b", "c"), ("c", "d"), ("d", "s"), ("x", "s"))
    assert click_depth(graph, "s") == {"s": 0, "a": 1, "b": 1, "c": 2, "d": 3}
    assert click_depth(graph, "unknown") == {}


def test_click_depth_takes_shortest_route():
    graph = g(("s", "a"), ("a", "b"), ("b", "c"), ("s", "c"))
    assert click_depth(graph, "s")["c"] == 1


def test_shortest_path():
    graph = g(("s", "a"), ("a", "b"), ("b", "c"), ("s", "x"), ("x", "c"), ("z", "s"))
    assert shortest_path(graph, "s", "c") == ["s", "x", "c"]
    assert shortest_path(graph, "s", "s") == ["s"]
    assert shortest_path(graph, "s", "z") == []
    assert shortest_path(graph, "s", "unknown") == []


def test_scc_simple():
    graph = g(("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("d", "e"), ("e", "d"), ("f", "a"))
    assert strongly_connected_components(graph) == [["a", "b", "c"], ["d", "e"], ["f"]]


def test_scc_singletons_sorted_and_cover_all_nodes():
    graph = g(("b", "c"), ("a", "b"), nodes=["z"])
    assert strongly_connected_components(graph) == [["a"], ["b"], ["c"], ["z"]]


def test_scc_handles_very_deep_chains_without_recursion_errors():
    count = 5000
    edges = [(f"n{i:05d}", f"n{i + 1:05d}") for i in range(count)]
    chain = LinkGraph.from_edges(edges)
    assert len(strongly_connected_components(chain)) == count + 1
    ring = LinkGraph.from_edges([*edges, (f"n{count:05d}", "n00000")])
    components = strongly_connected_components(ring)
    assert len(components) == 1 and len(components[0]) == count + 1


def test_dead_ends_and_sources():
    graph = g(("a", "b"), ("b", "c"), nodes=["lonely"])
    assert sorted(dead_ends(graph)) == ["c", "lonely"]
    assert sorted(sources(graph)) == ["a", "lonely"]


def test_from_site_skips_nofollow_external_and_broken():
    site = build_site([("home", "a"), ("home", "gone")], start="home", gone={"status": 404})
    site.add_link(Link(f"{BASE}/a", f"{BASE}/b", nofollow=True))
    site.add_link(Link(f"{BASE}/a", "https://other.example.org/", internal=False))
    site.ensure_link_targets()
    graph = LinkGraph.from_site(site)
    assert set(graph.nodes) == {f"{BASE}/", f"{BASE}/a", f"{BASE}/b"}
    assert not graph.has_edge(f"{BASE}/a", f"{BASE}/b")
    assert LinkGraph.from_site(site, follow_only=False).has_edge(f"{BASE}/a", f"{BASE}/b")
    assert f"{BASE}/gone" in LinkGraph.from_site(site, include_broken=True)


def test_from_site_resolves_redirects():
    site = build_site(
        [("home", "old"), ("new", "home")],
        start="home",
        old={"status": 301, "redirect_target": f"{BASE}/new"},
    )
    graph = LinkGraph.from_site(site)
    assert f"{BASE}/old" not in graph
    assert graph.has_edge(f"{BASE}/", f"{BASE}/new")
    raw = LinkGraph.from_site(site, resolve_redirects=False)
    assert raw.has_edge(f"{BASE}/", f"{BASE}/old")


def test_from_site_includes_sitemap_only_urls_and_is_sorted():
    site = build_site([("b", "a")])
    site.sitemap_urls.add(f"{BASE}/zzz")
    graph = LinkGraph.from_site(site)
    assert graph.nodes == sorted(graph.nodes)
    assert f"{BASE}/zzz" in graph
