"""Click paths: how a visitor (or crawler) gets from the homepage to a page.

``find_click_paths`` answers "why is this page four clicks deep?" by listing
the shortest chains of internal links from the start URL to each target, with
the anchor text used at every hop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from .analyzer import choose_start_url
from .graph import LinkGraph, all_shortest_paths
from .models import SiteData


@dataclass
class PathHop:
    """One page on a click path, with the anchor text of the link that led to it."""

    url: str
    anchor: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"url": self.url}
        if self.anchor:
            data["anchor"] = self.anchor
        return data


@dataclass
class ClickPath:
    """Shortest click paths from the start URL to one target."""

    query: str
    target: str
    found: bool = True
    depth: int | None = None
    inlinks: int = 0
    paths: list[list[PathHop]] = field(default_factory=list)

    @property
    def reachable(self) -> bool:
        return self.depth is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "target": self.target,
            "found": self.found,
            "reachable": self.reachable,
            "depth": self.depth,
            "inlinks": self.inlinks,
            "paths": [[hop.to_dict() for hop in path] for path in self.paths],
        }


def resolve_target(site: SiteData, graph: LinkGraph, query: str, start_url: str) -> str:
    """Map a user-supplied URL or path onto a node of ``graph`` where possible.

    Relative paths are resolved against the start URL, redirects are followed,
    and a missing or extra trailing slash is tolerated.  The (possibly
    unmatched) absolute URL is returned.
    """
    url = query.strip()
    if "://" not in url and start_url:
        url = urljoin(start_url, url)
    candidates = [url, site.resolve_redirect(url)]
    for candidate in list(candidates):
        if candidate.endswith("/"):
            candidates.append(candidate.rstrip("/"))
        else:
            candidates.append(candidate + "/")
    for candidate in candidates:
        if candidate in graph:
            return candidate
    return url


def _anchor_index(site: SiteData, follow_only: bool) -> dict[tuple[str, str], str]:
    anchors: dict[tuple[str, str], str] = {}
    for link in site.links:
        if not link.internal or (follow_only and link.nofollow):
            continue
        key = (link.source, site.resolve_redirect(link.target))
        text = " ".join(link.anchor.split())
        if key not in anchors or (not anchors[key] and text):
            anchors[key] = text
    return anchors


def find_click_paths(
    site: SiteData,
    targets: list[str],
    start_url: str = "",
    follow_only: bool = True,
    max_paths: int = 1,
) -> tuple[str, list[ClickPath]]:
    """Shortest click paths from the start URL to every target.

    Returns ``(start_url, results)``; results are in the order of ``targets``.
    ``max_paths`` caps how many equally short paths are listed per target.
    """
    site.ensure_link_targets()
    graph = LinkGraph.from_site(site, follow_only=follow_only)
    start = choose_start_url(site, graph, start_url)
    anchors = _anchor_index(site, follow_only)
    results: list[ClickPath] = []
    for query in targets:
        target = resolve_target(site, graph, query, start)
        result = ClickPath(query=query, target=target, found=target in graph)
        if result.found:
            result.inlinks = graph.in_degree(target)
            for path in all_shortest_paths(graph, start, target, limit=max_paths):
                hops = [PathHop(path[0])]
                hops.extend(
                    PathHop(url, anchors.get((previous, url), ""))
                    for previous, url in zip(path, path[1:], strict=False)
                )
                result.paths.append(hops)
            if result.paths:
                result.depth = len(result.paths[0]) - 1
        results.append(result)
    return start, results


def format_click_paths(start: str, results: list[ClickPath]) -> str:
    """Human-readable report of :func:`find_click_paths` results."""
    lines = [f"From: {start or '(no start URL)'}", ""]
    for result in results:
        if not result.found:
            lines.append(f"{result.target}  not found in the input")
        elif not result.reachable:
            lines.append(
                f"{result.target}  unreachable from the start URL "
                f"({result.inlinks} internal inlink{'s' if result.inlinks != 1 else ''})"
            )
        else:
            count = len(result.paths)
            suffix = f", {count} shortest paths shown" if count > 1 else ""
            lines.append(f"{result.target}  depth {result.depth}{suffix}")
            for number, path in enumerate(result.paths, 1):
                if count > 1:
                    lines.append(f"  path {number}:")
                indent = "    " if count > 1 else "  "
                lines.append(f"{indent}{path[0].url}")
                for hop in path[1:]:
                    anchor = f'  "{hop.anchor}"' if hop.anchor else "  (no anchor text)"
                    lines.append(f"{indent}-> {hop.url}{anchor}")
        lines.append("")
    reachable = sum(1 for result in results if result.reachable)
    lines.append(f"{reachable} of {len(results)} reachable")
    return "\n".join(lines) + "\n"
