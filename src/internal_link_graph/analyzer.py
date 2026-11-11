"""High level analysis: run every metric and collect SEO issues."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .anchors import AnchorProfile, anchor_profiles, is_generic_anchor, over_optimized
from .clusters import ClusterReport, TitleCluster, duplicate_title_clusters, topic_clusters
from .equity import EquityReport, link_equity_loss
from .graph import (
    LinkGraph,
    click_depth,
    hits,
    pagerank,
    strongly_connected_components,
)
from .models import SiteData

SEVERITIES = ("error", "warning", "notice")

#: Issue code -> (severity, human readable title).
ISSUE_TYPES: dict[str, tuple[str, str]] = {
    "broken_link": ("error", "Internal link to a broken page"),
    "redirect_chain": ("error", "Redirect chain with more than one hop"),
    "orphan_page": ("error", "Orphan page (no internal inlinks)"),
    "unreachable_page": ("error", "Page cannot be reached from the start URL"),
    "redirect_link": ("warning", "Internal link to a redirecting URL"),
    "deep_page": ("warning", "Page is many clicks away from the start URL"),
    "dead_end": ("warning", "Page without internal outlinks"),
    "nofollow_internal": ("warning", "Internal link flagged nofollow"),
    "linked_noindex": ("warning", "Internal links point at a noindex page"),
    "linked_canonicalized": ("warning", "Internal links point at a non-canonical URL"),
    "excessive_outlinks": ("warning", "Page has an excessive number of outlinks"),
    "over_optimized_anchor": ("warning", "One anchor text dominates the inlinks of a page"),
    "duplicate_title": ("warning", "Duplicate or near-duplicate title"),
    "generic_anchor": ("notice", "Generic anchor text"),
    "empty_anchor": ("notice", "Link without anchor text"),
    "low_inlinks": ("notice", "Page with very few internal inlinks"),
    "missing_title": ("notice", "Page without a title"),
    "missing_h1": ("notice", "Page without an H1"),
    "blocked_by_robots": ("notice", "Linked URL is blocked by robots.txt"),
}


@dataclass
class AnalysisConfig:
    """Thresholds and switches for :func:`analyze`."""

    damping: float = 0.85
    tolerance: float = 1e-10
    max_iterations: int = 200
    personalized: bool = False
    follow_only: bool = True
    max_outlinks: int = 100
    deep_threshold: int = 4
    low_inlinks: int = 1
    cluster_depth: int = 1
    title_similarity: float = 0.85
    anchor_min_links: int = 5
    anchor_dominance: float = 0.8
    start_url: str = ""


@dataclass
class Issue:
    """One finding. ``url`` is the page to fix; ``target`` an optional second URL."""

    code: str
    url: str
    detail: str = ""
    target: str = ""

    @property
    def severity(self) -> str:
        return ISSUE_TYPES.get(self.code, ("notice", ""))[0]

    @property
    def title(self) -> str:
        return ISSUE_TYPES.get(self.code, ("notice", self.code))[1]

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "url": self.url,
            "target": self.target,
            "detail": self.detail,
        }


@dataclass
class NodeMetrics:
    """Per-page metrics for every node of the link graph."""

    url: str
    status: int = 0
    title: str = ""
    indexable: bool = True
    in_sitemap: bool = False
    in_degree: int = 0
    out_degree: int = 0
    inlinks: int = 0
    outlinks: int = 0
    external_outlinks: int = 0
    nofollow_inlinks: int = 0
    pagerank: float = 0.0
    personalized_pagerank: float | None = None
    depth: int | None = None
    hub: float = 0.0
    authority: float = 0.0
    component: int = 0
    component_size: int = 1
    cluster: str = "/"
    word_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class AnalysisResult:
    """Everything :func:`analyze` computes."""

    site: SiteData
    graph: LinkGraph
    config: AnalysisConfig
    start_url: str = ""
    nodes: dict[str, NodeMetrics] = field(default_factory=dict)
    pagerank_iterations: int = 0
    pagerank_converged: bool = True
    orphans: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    dead_ends: list[str] = field(default_factory=list)
    components: list[list[str]] = field(default_factory=list)
    equity: EquityReport = field(default_factory=EquityReport)
    anchors: dict[str, AnchorProfile] = field(default_factory=dict)
    duplicate_titles: list[TitleCluster] = field(default_factory=list)
    clusters: ClusterReport = field(default_factory=ClusterReport)
    issues: list[Issue] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    # -- convenience views ---------------------------------------------------

    def pagerank_scores(self) -> dict[str, float]:
        return {url: node.pagerank for url, node in self.nodes.items()}

    def top_pages(self, limit: int = 10, key: str = "pagerank") -> list[NodeMetrics]:
        def sort_key(node: NodeMetrics) -> tuple[float, str]:
            value = getattr(node, key)
            return (-(value or 0.0), node.url)

        return sorted(self.nodes.values(), key=sort_key)[:limit]

    def issues_by_code(self) -> dict[str, list[Issue]]:
        grouped: dict[str, list[Issue]] = {}
        for issue in self.issues:
            grouped.setdefault(issue.code, []).append(issue)
        return grouped

    def issue_counts(self) -> dict[str, int]:
        counts = Counter(issue.code for issue in self.issues)
        return {code: counts[code] for code in ISSUE_TYPES if counts.get(code)}

    def depth_histogram(self) -> dict[int, int]:
        histogram: Counter[int] = Counter()
        for node in self.nodes.values():
            if node.depth is not None:
                histogram[node.depth] += 1
        return dict(sorted(histogram.items()))

    def to_dict(self, max_items: int | None = None) -> dict[str, Any]:
        """JSON-serialisable representation of the whole analysis."""

        def cut(items: list[Any]) -> list[Any]:
            return items if max_items is None else items[:max_items]

        ranked = sorted(self.nodes.values(), key=lambda node: (-node.pagerank, node.url))
        anchor_rows = sorted(self.anchors.values(), key=lambda p: (-p.total, p.target))
        return {
            "format": "internal-link-graph/analysis",
            "start_url": self.start_url,
            "summary": self.summary,
            "issue_counts": self.issue_counts(),
            "depth_histogram": {str(k): v for k, v in self.depth_histogram().items()},
            "nodes": [node.to_dict() for node in cut(ranked)],
            "orphans": cut(self.orphans),
            "unreachable": cut(self.unreachable),
            "dead_ends": cut(self.dead_ends),
            "components": [component for component in cut(self.components) if len(component) > 1],
            "equity": self.equity.to_dict(max_items),
            "anchors": [profile.to_dict() for profile in cut(anchor_rows)],
            "duplicate_titles": [cluster.to_dict() for cluster in cut(self.duplicate_titles)],
            "clusters": self.clusters.to_dict(),
            "issues": [issue.to_dict() for issue in cut(self.issues)],
        }


def choose_start_url(site: SiteData, graph: LinkGraph, preferred: str = "") -> str:
    """Pick the page click depth is measured from.

    Preference order: explicit setting, the site's recorded start URL, the
    shortest root-like URL, and finally the node with the most outlinks.
    """
    for candidate in (preferred, site.start_url):
        if candidate:
            resolved = site.resolve_redirect(candidate)
            if resolved in graph:
                return resolved
            if candidate in graph:
                return candidate
    if not graph.nodes:
        return ""
    roots = [url for url in graph.nodes if url.rstrip("/").count("/") == 2]
    if roots:
        return min(roots, key=lambda url: (len(url), url))
    return max(graph.nodes, key=lambda url: (graph.out_degree(url), -len(url), url))


def analyze(site: SiteData, config: AnalysisConfig | None = None) -> AnalysisResult:
    """Run the full analysis pipeline on ``site``."""
    config = config or AnalysisConfig()
    site.ensure_link_targets()
    graph = LinkGraph.from_site(site, follow_only=config.follow_only)
    start = choose_start_url(site, graph, config.start_url)
    result = AnalysisResult(site=site, graph=graph, config=config, start_url=start)

    ranks = pagerank(
        graph,
        damping=config.damping,
        tolerance=config.tolerance,
        max_iterations=config.max_iterations,
    )
    result.pagerank_iterations = ranks.iterations
    result.pagerank_converged = ranks.converged
    personalized: dict[str, float] = {}
    if config.personalized and start:
        personalized = pagerank(
            graph,
            damping=config.damping,
            tolerance=config.tolerance,
            max_iterations=config.max_iterations,
            personalization={start: 1.0},
        ).scores
    hub_scores = hits(graph, tolerance=config.tolerance, max_iterations=config.max_iterations)
    depths = click_depth(graph, start) if start else {}
    result.components = strongly_connected_components(graph)
    result.clusters = topic_clusters(graph, config.cluster_depth)

    component_of: dict[str, tuple[int, int]] = {}
    for number, component in enumerate(result.components):
        for url in component:
            component_of[url] = (number, len(component))

    raw_in: Counter[str] = Counter()
    raw_out: Counter[str] = Counter()
    external_out: Counter[str] = Counter()
    nofollow_in: Counter[str] = Counter()
    for link in site.links:
        if not link.internal:
            external_out[link.source] += 1
            continue
        if link.source == link.target:
            continue
        target = site.resolve_redirect(link.target)
        raw_out[link.source] += 1
        raw_in[target] += 1
        if link.nofollow:
            nofollow_in[target] += 1

    for url in graph.nodes:
        page = site.pages.get(url)
        component, component_size = component_of.get(url, (0, 1))
        result.nodes[url] = NodeMetrics(
            url=url,
            status=page.status if page else 0,
            title=page.title if page else "",
            indexable=page.indexable if page else True,
            in_sitemap=(page.in_sitemap if page else False) or url in site.sitemap_urls,
            in_degree=graph.in_degree(url),
            out_degree=graph.out_degree(url),
            inlinks=raw_in.get(url, 0),
            outlinks=raw_out.get(url, 0),
            external_outlinks=external_out.get(url, 0),
            nofollow_inlinks=nofollow_in.get(url, 0),
            pagerank=ranks.scores.get(url, 0.0),
            personalized_pagerank=personalized.get(url) if personalized else None,
            depth=depths.get(url),
            hub=hub_scores.hubs.get(url, 0.0),
            authority=hub_scores.authorities.get(url, 0.0),
            component=component,
            component_size=component_size,
            cluster=result.clusters.assignment.get(url, "/"),
            word_count=page.word_count if page else 0,
        )

    result.equity = link_equity_loss(site, ranks.scores, config.damping)
    redirect_map = {
        url: site.resolve_redirect(url) for url, page in site.pages.items() if page.is_redirect
    }
    result.anchors = anchor_profiles(site.links, resolve=redirect_map)
    titles = {
        url: page.title
        for url, page in site.pages.items()
        if url in graph and page.title and page.indexable
    }
    result.duplicate_titles = duplicate_title_clusters(titles, threshold=config.title_similarity)

    _collect_structure(result)
    _collect_issues(result)
    result.summary = _summarise(result)
    return result


def _collect_structure(result: AnalysisResult) -> None:
    """Orphans, unreachable pages and dead ends."""
    start = result.start_url
    from_crawl = result.site.meta.get("source") == "crawl"
    for url, node in result.nodes.items():
        page = result.site.pages.get(url)
        if page is not None and (page.blocked_by_robots or not page.is_ok):
            continue
        if url != start and node.in_degree == 0:
            result.orphans.append(url)
        elif url != start and start and node.depth is None:
            result.unreachable.append(url)
        if node.out_degree > 0:
            continue
        # After a crawl only fetched pages can be called dead ends: for URLs beyond
        # the crawl limits we simply do not know the outlinks.
        if from_crawl and (page is None or not page.crawled):
            continue
        if page is not None and page.content_type and "html" not in page.content_type:
            continue
        result.dead_ends.append(url)
    result.orphans.sort()
    result.unreachable.sort()
    result.dead_ends.sort()


def _collect_issues(result: AnalysisResult) -> None:
    site = result.site
    config = result.config
    issues = result.issues

    def add(code: str, url: str, detail: str = "", target: str = "") -> None:
        issues.append(Issue(code=code, url=url, detail=detail, target=target))

    # Link level issues, de-duplicated per (source, target).
    reported: set[tuple[str, str, str]] = set()
    for link in site.links:
        if not link.internal or link.source == link.target:
            continue
        target_page = site.pages.get(link.target)

        def once(code: str, detail: str, _link=link) -> None:
            key = (code, _link.source, _link.target)
            if key not in reported:
                reported.add(key)
                add(code, _link.source, detail, _link.target)

        if target_page is not None:
            if target_page.is_broken:
                status = target_page.status or target_page.error or "error"
                once("broken_link", f"target returned {status}")
            elif target_page.is_redirect:
                final = site.resolve_redirect(link.target)
                once("redirect_link", f"{target_page.status} redirect to {final}")
            elif target_page.blocked_by_robots:
                once("blocked_by_robots", "target is disallowed by robots.txt")
        if link.nofollow:
            once("nofollow_internal", "rel=" + (" ".join(link.rel) or "nofollow (page level)"))
        if not link.anchor.strip():
            once("empty_anchor", "image without alt text" if link.is_image else "no anchor text")
        elif is_generic_anchor(link.anchor):
            once("generic_anchor", f'anchor "{link.anchor.strip()}"')

    # Page level issues.
    for url in sorted(site.pages):
        page = site.pages[url]
        if page.is_redirect and len(page.redirect_chain) > 2:
            hops = len(page.redirect_chain) - 1
            add("redirect_chain", url, f"{hops} hops: " + " -> ".join(page.redirect_chain))
    for url in result.orphans:
        node = result.nodes[url]
        if node.inlinks > 0:
            detail = "only linked through nofollow links"
        elif node.in_sitemap:
            detail = "listed in the sitemap but never linked"
        else:
            detail = "never linked"
        add("orphan_page", url, detail)
    for url in result.unreachable:
        add("unreachable_page", url, "has inlinks, but none on a path from the start URL")
    for url in result.dead_ends:
        add("dead_end", url, "no followed internal links leave this page")

    for url in sorted(result.nodes):
        node = result.nodes[url]
        page = site.pages.get(url)
        if node.depth is not None and node.depth >= config.deep_threshold:
            add("deep_page", url, f"{node.depth} clicks from the start URL")
        total_out = node.outlinks + node.external_outlinks
        if total_out > config.max_outlinks:
            add(
                "excessive_outlinks",
                url,
                f"{total_out} links ({node.outlinks} internal, {node.external_outlinks} external)",
            )
        if (
            url != result.start_url
            and 0 < node.in_degree <= config.low_inlinks
            and node.indexable
            and (page is None or page.is_ok)
        ):
            add("low_inlinks", url, f"only {node.in_degree} linking page(s)")
        if page is None or not page.crawled or page.status != 200:
            continue
        if node.in_degree > 0 and page.noindex:
            add("linked_noindex", url, f"{node.in_degree} linking page(s), page is noindex")
        if node.in_degree > 0 and page.canonicalized:
            add("linked_canonicalized", url, f"canonical is {page.canonical}", page.canonical)
        is_html = not page.content_type or "html" in page.content_type
        if is_html and not page.blocked_by_robots:
            if not page.title:
                add("missing_title", url)
            if not page.h1:
                add("missing_h1", url)

    for profile in over_optimized(
        result.anchors, min_links=config.anchor_min_links, dominance=config.anchor_dominance
    ):
        add(
            "over_optimized_anchor",
            profile.target,
            f'"{profile.top_anchor}" is used by {profile.top_share:.0%} of '
            f"{profile.total - profile.empty} text links",
        )
    for cluster in result.duplicate_titles:
        kind = "identical" if cluster.exact else "near-duplicate"
        for url, title in zip(cluster.urls, cluster.titles, strict=True):
            add("duplicate_title", url, f'{kind} title shared by {cluster.size} pages: "{title}"')

    order = {code: position for position, code in enumerate(ISSUE_TYPES)}
    severity_rank = {name: position for position, name in enumerate(SEVERITIES)}
    issues.sort(
        key=lambda issue: (
            severity_rank[issue.severity],
            order.get(issue.code, len(order)),
            issue.url,
            issue.target,
        )
    )


def _summarise(result: AnalysisResult) -> dict[str, Any]:
    site = result.site
    graph = result.graph
    nodes = result.nodes
    depths = [node.depth for node in nodes.values() if node.depth is not None]
    statuses: Counter[str] = Counter()
    for page in site.pages.values():
        if page.status:
            statuses[f"{page.status // 100}xx"] += 1
        elif page.crawled and page.blocked_by_robots:
            statuses["blocked"] += 1
        elif page.crawled:
            statuses["error"] += 1
        else:
            statuses["not_crawled"] += 1
    internal_links = sum(1 for link in site.links if link.internal)
    severity_counts = Counter(issue.severity for issue in result.issues)
    count = len(nodes)
    return {
        "start_url": result.start_url,
        "pages": count,
        "urls_known": len(site.pages),
        "indexable_pages": sum(1 for node in nodes.values() if node.indexable),
        "edges": graph.edge_count,
        "internal_links": internal_links,
        "external_links": len(site.links) - internal_links,
        "nofollow_internal_links": sum(1 for link in site.links if link.internal and link.nofollow),
        "sitemap_urls": len(site.sitemap_urls),
        "status_codes": dict(sorted(statuses.items())),
        "average_in_degree": round(graph.edge_count / count, 3) if count else 0.0,
        "density": round(graph.density(), 6),
        "reciprocity": round(graph.reciprocity(), 4),
        "max_depth": max(depths) if depths else 0,
        "average_depth": round(sum(depths) / len(depths), 3) if depths else 0.0,
        "reachable_pages": len(depths),
        "orphans": len(result.orphans),
        "unreachable": len(result.unreachable),
        "dead_ends": len(result.dead_ends),
        "components": len(result.components),
        "largest_component": len(result.components[0]) if result.components else 0,
        "clusters": len(result.clusters.members),
        "duplicate_title_clusters": len(result.duplicate_titles),
        "equity_lost": round(result.equity.total, 6),
        "equity_lost_by_kind": {k: round(v, 6) for k, v in result.equity.totals.items()},
        "pagerank_iterations": result.pagerank_iterations,
        "pagerank_converged": result.pagerank_converged,
        "issues": len(result.issues),
        "issues_by_severity": {name: severity_counts.get(name, 0) for name in SEVERITIES},
    }
