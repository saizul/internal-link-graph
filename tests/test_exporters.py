import csv
import io
import json
import xml.etree.ElementTree as ET

import pytest

from conftest import BASE, build_site
from internal_link_graph.analyzer import analyze
from internal_link_graph.exporters import (
    EDGE_COLUMNS,
    ISSUE_COLUMNS,
    NODE_COLUMNS,
    SUGGESTION_COLUMNS,
    analysis_to_json,
    csv_string,
    to_dot,
    to_graphml,
    write_csv_bundle,
    write_edges_csv,
    write_issues_csv,
    write_nodes_csv,
    write_suggestions_csv,
)
from internal_link_graph.models import Link
from internal_link_graph.suggest import Suggestion

HOME = f"{BASE}/"
NS = {"g": "http://graphml.graphdrawing.org/xmlns"}


def u(name: str) -> str:
    return f"{BASE}/{name}"


@pytest.fixture
def result(small_site):
    return analyze(small_site)


def rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def sample_suggestion() -> Suggestion:
    return Suggestion(
        source=u("a"), target=u("o"), score=0.5, similarity=0.4, source_pagerank=0.3,
        target_depth=None, target_inlinks=0, anchor="Title of o",
        shared_terms=["title", "page"], reason="no internal inlinks",
    )


def test_json_export_round_trips(result):
    data = json.loads(analysis_to_json(result))
    assert data["format"] == "internal-link-graph/analysis"
    assert data["summary"]["pages"] == 5
    assert "suggestions" not in data
    with_suggestions = json.loads(analysis_to_json(result, [sample_suggestion()], indent=None))
    assert with_suggestions["suggestions"][0]["target"] == u("o")
    assert len(json.loads(analysis_to_json(result, max_items=1))["nodes"]) == 1


def test_json_export_keeps_non_ascii_text(small_site):
    small_site.pages[u("a")].title = "গোলাপ গাছের যত্ন"
    assert "গোলাপ" in analysis_to_json(analyze(small_site))


def test_nodes_csv_has_all_columns_sorted_by_pagerank(result):
    text = csv_string(write_nodes_csv, result)
    parsed = rows(text)
    assert tuple(parsed[0]) == NODE_COLUMNS
    assert len(parsed) == 5
    scores = [float(row["pagerank"]) for row in parsed]
    assert scores == sorted(scores, reverse=True)
    assert parsed[0]["url"] == u("a")
    orphan = next(row for row in parsed if row["url"] == u("o"))
    assert orphan["depth"] == "" and orphan["in_degree"] == "0"
    assert orphan["indexable"] == "true" and orphan["personalized_pagerank"] == ""


def test_edges_csv_lists_internal_links_only_by_default(small_site):
    small_site.add_link(Link(HOME, "https://other.example.org/", anchor="Other", internal=False))
    small_site.links[0].rel = ("nofollow", "noopener")
    small_site.links[0].nofollow = True
    result = analyze(small_site)
    parsed = rows(csv_string(write_edges_csv, result))
    assert tuple(parsed[0]) == EDGE_COLUMNS and len(parsed) == 7
    assert parsed[0]["nofollow"] == "true" and parsed[0]["rel"] == "nofollow noopener"
    buffer = io.StringIO()
    assert write_edges_csv(result, buffer, include_external=True) == 8
    assert rows(buffer.getvalue())[-1]["internal"] == "false"


def test_issues_csv(result):
    buffer = io.StringIO()
    count = write_issues_csv(result, buffer)
    parsed = rows(buffer.getvalue())
    assert count == len(result.issues) == len(parsed)
    assert tuple(parsed[0]) == ISSUE_COLUMNS
    assert {"orphan_page", "low_inlinks"} <= {row["code"] for row in parsed}


def test_suggestions_csv(result):
    buffer = io.StringIO()
    assert write_suggestions_csv([sample_suggestion()], buffer) == 1
    parsed = rows(buffer.getvalue())
    assert tuple(parsed[0]) == SUGGESTION_COLUMNS
    assert parsed[0]["target_depth"] == "" and parsed[0]["anchor"] == "Title of o"
    assert "title" in parsed[0]["shared_terms"] and "page" in parsed[0]["shared_terms"]


def test_csv_escapes_commas_quotes_and_newlines(small_site):
    small_site.pages[u("a")].title = 'Roses, "old" and new\nsecond line'
    parsed = rows(csv_string(write_nodes_csv, analyze(small_site)))
    row = next(row for row in parsed if row["url"] == u("a"))
    assert row["title"] == 'Roses, "old" and new\nsecond line'


def test_csv_bundle_writes_files(result, tmp_path):
    written = write_csv_bundle(result, tmp_path / "out" / "nested")
    assert set(written) == {"nodes", "edges", "issues"}
    assert all(path.is_file() for path in written.values())
    assert len(rows(written["edges"].read_text(encoding="utf-8"))) == 7
    with_suggestions = write_csv_bundle(result, tmp_path / "out2", [sample_suggestion()])
    assert with_suggestions["suggestions"].name == "suggestions.csv"


def test_graphml_is_well_formed_and_complete(result):
    root = ET.fromstring(to_graphml(result))
    assert root.tag == "{http://graphml.graphdrawing.org/xmlns}graphml"
    graph = root.find("g:graph", NS)
    assert graph.get("edgedefault") == "directed"
    nodes = graph.findall("g:node", NS)
    edges = graph.findall("g:edge", NS)
    assert {node.get("id") for node in nodes} == set(result.nodes)
    assert len(edges) == result.graph.edge_count == 7
    assert (HOME, u("a")) in {(edge.get("source"), edge.get("target")) for edge in edges}
    declared = {key.get("id") for key in root.findall("g:key", NS)}
    used = {data.get("key") for data in root.iter("{http://graphml.graphdrawing.org/xmlns}data")}
    assert used <= declared


def test_graphml_node_and_edge_data(result):
    root = ET.fromstring(to_graphml(result))
    nodes = {node.get("id"): node for node in root.iter(f"{{{NS['g']}}}node")}
    data = {item.get("key"): item.text for item in nodes[u("a")].findall("g:data", NS)}
    assert float(data["n_pagerank"]) == pytest.approx(result.nodes[u("a")].pagerank, abs=1e-6)
    assert data["n_depth"] == "1" and data["n_in_degree"] == "3"
    assert data["n_title"] == "Title of a" and data["n_indexable"] == "true"
    orphan = {item.get("key") for item in nodes[u("o")].findall("g:data", NS)}
    assert "n_depth" not in orphan
    edge = next(iter(root.iter(f"{{{NS['g']}}}edge")))
    values = {item.get("key"): item.text for item in edge.findall("g:data", NS)}
    assert values["e_weight"] == "1" and values["e_anchor"]


def test_graphml_escapes_special_characters():
    site = build_site([("home", 'search?q=a&b=<c>"d"'), ('search?q=a&b=<c>"d"', "home")],
                      start="home")
    site.pages[HOME].title = "Tools & <Tips> \x07"
    site.pages[HOME].status = 200
    site.links[0].anchor = 'Find "a" & <b>'
    root = ET.fromstring(to_graphml(analyze(site)))
    ids = {node.get("id") for node in root.iter(f"{{{NS['g']}}}node")}
    assert u('search?q=a&b=<c>"d"') in ids
    texts = [item.text for item in root.iter(f"{{{NS['g']}}}data")]
    assert "Tools & <Tips> " in texts and 'Find "a" & <b>' in texts


def test_dot_export_structure(result):
    dot = to_dot(result)
    assert dot.startswith("digraph internal_links {") and dot.rstrip().endswith("}")
    assert dot.count(" -> ") == 7
    assert f'"{HOME}" -> "{u("a")}"' in dot
    home_line = next(line for line in dot.splitlines() if line.startswith(f'  "{HOME}" ['))
    assert "penwidth=2" in home_line
    orphan_line = next(line for line in dot.splitlines() if line.startswith(f'  "{u("o")}" ['))
    assert "#fde3e3" in orphan_line
    assert dot.count("{") == dot.count("}") == 1


def test_dot_max_nodes_keeps_top_pages_and_their_edges(result):
    dot = to_dot(result, max_nodes=2)
    node_lines = [line for line in dot.splitlines() if "label=" in line and "->" not in line]
    assert len(node_lines) == 2
    assert all(line.split('"')[1] in {u("a"), u("b")} for line in node_lines)
    assert dot.count(" -> ") == 1
    assert to_dot(result, max_nodes=None).count("label=") == 5


def test_dot_quotes_and_repeated_links():
    site = build_site([("home", 'a"b'), ("home", 'a"b'), ('a"b', "home")], start="home")
    site.pages[HOME].title = 'Say "hi"\\now'
    site.pages[HOME].status = 200
    dot = to_dot(analyze(site))
    assert 'a\\"b' in dot and 'Say \\"hi\\"\\\\now' in dot
    assert "[penwidth=1.5]" in dot


def test_dot_of_empty_analysis():
    dot = to_dot(analyze(build_site([])))
    assert "->" not in dot and dot.rstrip().endswith("}")
