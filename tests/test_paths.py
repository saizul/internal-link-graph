import json

from conftest import BASE, build_site
from internal_link_graph import ClickPath, all_shortest_paths, find_click_paths
from internal_link_graph.graph import LinkGraph
from internal_link_graph.models import Link
from internal_link_graph.paths import format_click_paths
from test_cli import run

HOME = f"{BASE}/"


def g(*edges):
    return LinkGraph.from_edges(edges)


def test_all_shortest_paths_lists_every_tie():
    graph = g(("s", "a"), ("s", "b"), ("a", "c"), ("b", "c"), ("c", "d"), ("s", "x"), ("x", "y"),
              ("y", "d"))
    assert all_shortest_paths(graph, "s", "d") == [
        ["s", "a", "c", "d"],
        ["s", "b", "c", "d"],
        ["s", "x", "y", "d"],
    ]


def test_all_shortest_paths_limit_and_edge_cases():
    graph = g(("s", "a"), ("s", "b"), ("a", "c"), ("b", "c"), ("z", "s"))
    assert len(all_shortest_paths(graph, "s", "c", limit=1)) == 1
    assert all_shortest_paths(graph, "s", "c", limit=0) == []
    assert all_shortest_paths(graph, "s", "s") == [["s"]]
    assert all_shortest_paths(graph, "s", "z") == []
    assert all_shortest_paths(graph, "s", "unknown") == []
    assert all_shortest_paths(graph, "unknown", "s") == []


def test_all_shortest_paths_unlimited():
    # A 2x2x2 diamond chain has 8 shortest paths.
    edges = []
    for layer in range(3):
        for left in ("l", "r"):
            edges.append((f"n{layer}", f"{left}{layer}"))
            edges.append((f"{left}{layer}", f"n{layer + 1}"))
    graph = g(*edges)
    assert len(all_shortest_paths(graph, "n0", "n3", limit=None)) == 8


def site():
    s = build_site(
        [("home", "blog/"), ("home", "about"), ("blog/", "blog/post"), ("about", "blog/post"),
         ("blog/post", "blog/deep"), ("orphan", "home")],
        start="home",
    )
    s.add_link(Link(source=HOME, target=f"{BASE}/secret", anchor="Secret", nofollow=True))
    s.ensure_link_targets()
    return s


def test_find_click_paths_with_anchors():
    start, results = find_click_paths(site(), [f"{BASE}/blog/deep"])
    assert start == HOME
    (result,) = results
    assert isinstance(result, ClickPath)
    assert result.reachable and result.depth == 3
    hops = result.paths[0]
    assert [hop.url for hop in hops] == [HOME, f"{BASE}/about", f"{BASE}/blog/post",
                                         f"{BASE}/blog/deep"]
    assert hops[0].anchor == ""
    assert hops[-1].anchor == "blog/deep"


def test_find_click_paths_multiple_and_relative_targets():
    _start, results = find_click_paths(site(), ["/blog/post", "blog/post"], max_paths=5)
    assert results[0].target == f"{BASE}/blog/post"
    assert len(results[0].paths) == 2
    assert results[0].depth == 2
    assert results[0].inlinks == 2
    assert results[1].target == f"{BASE}/blog/post"


def test_trailing_slash_is_tolerated():
    _start, results = find_click_paths(site(), [f"{BASE}/blog"])
    assert results[0].target == f"{BASE}/blog/"
    assert results[0].depth == 1


def test_unreachable_missing_and_nofollow():
    _start, results = find_click_paths(site(), ["/orphan", "/nope", "/secret"])
    orphan, missing, secret = results
    assert orphan.found and not orphan.reachable and orphan.inlinks == 0
    assert not missing.found and not missing.reachable
    assert secret.found and not secret.reachable
    _start, results = find_click_paths(site(), ["/secret"], follow_only=False)
    assert results[0].depth == 1 and results[0].paths[0][-1].anchor == "Secret"


def test_redirect_targets_resolve_to_destination():
    s = build_site([("home", "old"), ("home", "new")], start="home")
    s.page(f"{BASE}/old").status = 301
    s.page(f"{BASE}/old").redirect_target = f"{BASE}/new"
    _start, results = find_click_paths(s, ["/old"])
    assert results[0].target == f"{BASE}/new"
    assert results[0].depth == 1


def test_format_click_paths():
    start, results = find_click_paths(site(), ["/blog/post", "/orphan", "/nope"], max_paths=2)
    text = format_click_paths(start, results)
    assert text.startswith(f"From: {HOME}\n")
    assert f"{BASE}/blog/post  depth 2, 2 shortest paths shown" in text
    assert "  path 1:" in text
    assert f'-> {BASE}/blog/post  "blog/post"' in text
    assert f"{BASE}/orphan  unreachable from the start URL (0 internal inlinks)" in text
    assert f"{BASE}/nope  not found in the input" in text
    assert text.rstrip().endswith("1 of 3 reachable")


def test_cli_path_text_and_json(tmp_path):
    path = tmp_path / "site.json"
    site().save(path)
    code, out, _err = run("path", path, "/blog/deep")
    assert code == 0
    assert "depth 3" in out
    code, out, _err = run("path", path, "/blog/deep", "/orphan", "--output-format", "json")
    assert code == 0
    payload = json.loads(out)
    assert payload["start_url"] == HOME
    assert [r["depth"] for r in payload["results"]] == [3, None]
    assert payload["results"][0]["paths"][0][1] == {"url": f"{BASE}/about", "anchor": "about"}


def test_cli_path_max_depth_exit_code(tmp_path):
    path = tmp_path / "site.json"
    site().save(path)
    assert run("path", path, "/blog/post", "--max-depth", "2")[0] == 0
    assert run("path", path, "/blog/deep", "--max-depth", "2")[0] == 3
    assert run("path", path, "/orphan", "--max-depth", "5")[0] == 3


def test_cli_path_targets_file_and_errors(tmp_path):
    path = tmp_path / "site.json"
    site().save(path)
    targets = tmp_path / "targets.txt"
    targets.write_text("# pages to check\n/blog/post\n\n/about\n", encoding="utf-8")
    code, out, _err = run("path", path, "--targets-file", targets)
    assert code == 0 and "2 of 2 reachable" in out
    code, _out, err = run("path", path)
    assert code == 1 and "at least one target" in err
    code, _out, err = run("path", tmp_path / "missing.json", "/")
    assert code == 1 and "not found" in err
