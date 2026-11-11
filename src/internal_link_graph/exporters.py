"""Machine readable exports: JSON, CSV, GraphML and Graphviz DOT."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, TextIO
from xml.sax.saxutils import escape, quoteattr

from .analyzer import AnalysisResult
from .suggest import Suggestion
from .urlnorm import short_path

NODE_COLUMNS = (
    "url",
    "status",
    "title",
    "indexable",
    "in_sitemap",
    "in_degree",
    "out_degree",
    "inlinks",
    "outlinks",
    "external_outlinks",
    "nofollow_inlinks",
    "pagerank",
    "personalized_pagerank",
    "depth",
    "hub",
    "authority",
    "component",
    "component_size",
    "cluster",
    "word_count",
)
EDGE_COLUMNS = ("source", "target", "anchor", "rel", "nofollow", "position", "is_image", "internal")
ISSUE_COLUMNS = ("severity", "code", "url", "target", "detail")
SUGGESTION_COLUMNS = (
    "source",
    "target",
    "anchor",
    "score",
    "similarity",
    "source_pagerank",
    "target_depth",
    "target_inlinks",
    "shared_terms",
    "reason",
)


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.8g}"
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    return value


def _write_csv(handle: TextIO, columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> int:
    writer = csv.writer(handle, lineterminator="\n")
    writer.writerow(columns)
    count = 0
    for row in rows:
        writer.writerow([_cell(row.get(column)) for column in columns])
        count += 1
    return count


# -- JSON --------------------------------------------------------------------


def analysis_to_json(
    result: AnalysisResult,
    suggestions: Sequence[Suggestion] | None = None,
    indent: int | None = 2,
    max_items: int | None = None,
) -> str:
    """Serialise an analysis (and optional suggestions) to a JSON string."""
    payload = result.to_dict(max_items=max_items)
    if suggestions is not None:
        payload["suggestions"] = [suggestion.to_dict() for suggestion in suggestions]
    return json.dumps(payload, ensure_ascii=False, indent=indent)


# -- CSV ---------------------------------------------------------------------


def write_nodes_csv(result: AnalysisResult, handle: TextIO) -> int:
    ranked = sorted(result.nodes.values(), key=lambda node: (-node.pagerank, node.url))
    return _write_csv(handle, NODE_COLUMNS, (node.to_dict() for node in ranked))


def write_edges_csv(result: AnalysisResult, handle: TextIO, include_external: bool = False) -> int:
    rows = []
    for link in result.site.links:
        if not link.internal and not include_external:
            continue
        rows.append(
            {
                "source": link.source,
                "target": link.target,
                "anchor": link.anchor,
                "rel": link.rel,
                "nofollow": link.nofollow,
                "position": link.position,
                "is_image": link.is_image,
                "internal": link.internal,
            }
        )
    return _write_csv(handle, EDGE_COLUMNS, rows)


def write_issues_csv(result: AnalysisResult, handle: TextIO) -> int:
    return _write_csv(handle, ISSUE_COLUMNS, (issue.to_dict() for issue in result.issues))


def write_suggestions_csv(suggestions: Iterable[Suggestion], handle: TextIO) -> int:
    return _write_csv(handle, SUGGESTION_COLUMNS, (s.to_dict() for s in suggestions))


def write_csv_bundle(
    result: AnalysisResult,
    directory: str | Path,
    suggestions: Sequence[Suggestion] | None = None,
) -> dict[str, Path]:
    """Write ``nodes.csv``, ``edges.csv``, ``issues.csv`` (and ``suggestions.csv``)."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    writers = {
        "nodes": lambda handle: write_nodes_csv(result, handle),
        "edges": lambda handle: write_edges_csv(result, handle),
        "issues": lambda handle: write_issues_csv(result, handle),
    }
    if suggestions is not None:
        writers["suggestions"] = lambda handle: write_suggestions_csv(suggestions, handle)
    for name, writer in writers.items():
        path = target / f"{name}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer(handle)
        written[name] = path
    return written


def csv_string(writer: Any, *args: Any) -> str:
    """Run one of the ``write_*_csv`` functions into a string."""
    buffer = io.StringIO()
    writer(*args, buffer)
    return buffer.getvalue()


# -- GraphML -----------------------------------------------------------------

_GRAPHML_NODE_KEYS = (
    ("status", "int"),
    ("title", "string"),
    ("pagerank", "double"),
    ("depth", "int"),
    ("in_degree", "int"),
    ("out_degree", "int"),
    ("hub", "double"),
    ("authority", "double"),
    ("component", "int"),
    ("cluster", "string"),
    ("indexable", "boolean"),
)


def _xml_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    # XML 1.0 cannot represent most control characters at all.
    text = "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 0x20)
    return escape(text)


def to_graphml(result: AnalysisResult) -> str:
    """GraphML document (readable by Gephi, yEd, Cytoscape, NetworkX...)."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
    ]
    for name, kind in _GRAPHML_NODE_KEYS:
        lines.append(
            f'  <key id="n_{name}" for="node" attr.name="{name}" attr.type="{kind}"/>'
        )
    lines.append('  <key id="e_weight" for="edge" attr.name="weight" attr.type="int"/>')
    lines.append('  <key id="e_anchor" for="edge" attr.name="anchor" attr.type="string"/>')
    lines.append('  <graph id="internal-links" edgedefault="directed">')
    anchors: dict[tuple[str, str], str] = {}
    for link in result.site.links:
        if link.internal and link.anchor:
            key = (link.source, result.site.resolve_redirect(link.target))
            anchors.setdefault(key, link.anchor)
    for url in result.graph.nodes:
        node = result.nodes[url]
        lines.append(f"    <node id={quoteattr(url)}>")
        data = node.to_dict()
        for name, _kind in _GRAPHML_NODE_KEYS:
            value = data.get(name)
            if value is None or value == "":
                continue
            lines.append(f'      <data key="n_{name}">{_xml_text(value)}</data>')
        lines.append("    </node>")
    for number, (source, target, count) in enumerate(result.graph.edges()):
        lines.append(
            f'    <edge id="e{number}" source={quoteattr(source)} target={quoteattr(target)}>'
        )
        lines.append(f'      <data key="e_weight">{count}</data>')
        anchor = anchors.get((source, target))
        if anchor:
            lines.append(f'      <data key="e_anchor">{_xml_text(anchor)}</data>')
        lines.append("    </edge>")
    lines.append("  </graph>")
    lines.append("</graphml>")
    return "\n".join(lines) + "\n"


# -- DOT ---------------------------------------------------------------------


def _dot_quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


def to_dot(result: AnalysisResult, max_nodes: int | None = 200, label_length: int = 40) -> str:
    """Graphviz DOT export.

    Large graphs are unreadable when drawn, so only the ``max_nodes`` pages
    with the highest PageRank are included (``None`` exports everything).
    Node size scales with PageRank; orphans and dead ends are highlighted.
    """
    ranked = sorted(result.nodes.values(), key=lambda node: (-node.pagerank, node.url))
    if max_nodes is not None:
        ranked = ranked[:max_nodes]
    included = {node.url for node in ranked}
    top_rank = ranked[0].pagerank if ranked else 0.0
    orphans = set(result.orphans)
    dead = set(result.dead_ends)
    lines = [
        "digraph internal_links {",
        "  rankdir=LR;",
        '  node [shape=box, style="rounded,filled", fillcolor="#eef4fd", fontsize=10];',
        '  edge [color="#8a8a8a", arrowsize=0.6];',
    ]
    for node in ranked:
        attributes = [f"label={_dot_quote(short_path(node.url, label_length))}"]
        scale = node.pagerank / top_rank if top_rank > 0 else 0.0
        attributes.append(f"width={0.6 + 1.4 * scale:.2f}")
        attributes.append(f"tooltip={_dot_quote(node.title or node.url)}")
        if node.url == result.start_url:
            attributes.append('fillcolor="#cde2fb"')
            attributes.append("penwidth=2")
        elif node.url in orphans:
            attributes.append('fillcolor="#fde3e3"')
        elif node.url in dead:
            attributes.append('fillcolor="#fdf0cf"')
        lines.append(f"  {_dot_quote(node.url)} [{', '.join(attributes)}];")
    for source, target, count in result.graph.edges():
        if source in included and target in included:
            suffix = f" [penwidth={min(1 + 0.5 * (count - 1), 4):.1f}]" if count > 1 else ""
            lines.append(f"  {_dot_quote(source)} -> {_dot_quote(target)}{suffix};")
    lines.append("}")
    return "\n".join(lines) + "\n"
