"""Compare two crawls of the same site."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .analyzer import AnalysisConfig, AnalysisResult, analyze
from .models import SiteData


@dataclass
class Mover:
    """A page whose PageRank changed between two crawls."""

    url: str
    old: float
    new: float

    @property
    def delta(self) -> float:
        return self.new - self.old

    @property
    def relative(self) -> float:
        """Relative change (``0.25`` = +25%); ``inf`` semantics avoided for new pages."""
        return self.delta / self.old if self.old > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "old": self.old,
            "new": self.new,
            "delta": self.delta,
            "relative": round(self.relative, 6),
        }


@dataclass
class CrawlDiff:
    """Differences between an old and a new crawl."""

    new_pages: list[str] = field(default_factory=list)
    lost_pages: list[str] = field(default_factory=list)
    new_links: list[tuple[str, str]] = field(default_factory=list)
    lost_links: list[tuple[str, str]] = field(default_factory=list)
    gainers: list[Mover] = field(default_factory=list)
    losers: list[Mover] = field(default_factory=list)
    depth_changes: list[tuple[str, int | None, int | None]] = field(default_factory=list)
    status_changes: list[tuple[str, int, int]] = field(default_factory=list)
    new_orphans: list[str] = field(default_factory=list)
    fixed_orphans: list[str] = field(default_factory=list)
    issue_changes: dict[str, tuple[int, int]] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.new_pages
            or self.lost_pages
            or self.new_links
            or self.lost_links
            or self.depth_changes
            or self.status_changes
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "internal-link-graph/diff",
            "summary": self.summary,
            "new_pages": self.new_pages,
            "lost_pages": self.lost_pages,
            "new_links": [{"source": s, "target": t} for s, t in self.new_links],
            "lost_links": [{"source": s, "target": t} for s, t in self.lost_links],
            "pagerank_gainers": [mover.to_dict() for mover in self.gainers],
            "pagerank_losers": [mover.to_dict() for mover in self.losers],
            "depth_changes": [
                {"url": url, "old": old, "new": new} for url, old, new in self.depth_changes
            ],
            "status_changes": [
                {"url": url, "old": old, "new": new} for url, old, new in self.status_changes
            ],
            "new_orphans": self.new_orphans,
            "fixed_orphans": self.fixed_orphans,
            "issue_changes": {
                code: {"old": old, "new": new} for code, (old, new) in self.issue_changes.items()
            },
        }


def diff_analyses(old: AnalysisResult, new: AnalysisResult, top: int = 20) -> CrawlDiff:
    """Compare two finished analyses."""
    result = CrawlDiff()
    old_nodes, new_nodes = old.nodes, new.nodes
    result.new_pages = sorted(set(new_nodes) - set(old_nodes))
    result.lost_pages = sorted(set(old_nodes) - set(new_nodes))

    old_edges = {(s, t) for s, t, _ in old.graph.edges()}
    new_edges = {(s, t) for s, t, _ in new.graph.edges()}
    result.new_links = sorted(new_edges - old_edges)
    result.lost_links = sorted(old_edges - new_edges)

    movers = []
    for url in set(old_nodes) & set(new_nodes):
        mover = Mover(url=url, old=old_nodes[url].pagerank, new=new_nodes[url].pagerank)
        if abs(mover.delta) > 1e-12:
            movers.append(mover)
        old_depth, new_depth = old_nodes[url].depth, new_nodes[url].depth
        if old_depth != new_depth:
            result.depth_changes.append((url, old_depth, new_depth))
    result.gainers = sorted(
        (m for m in movers if m.delta > 0), key=lambda m: (-m.delta, m.url)
    )[:top]
    result.losers = sorted((m for m in movers if m.delta < 0), key=lambda m: (m.delta, m.url))[:top]
    result.depth_changes.sort()

    for url in sorted(set(old.site.pages) & set(new.site.pages)):
        before = old.site.pages[url].status
        after = new.site.pages[url].status
        if before != after and before and after:
            result.status_changes.append((url, before, after))

    old_orphans, new_orphans = set(old.orphans), set(new.orphans)
    result.new_orphans = sorted(new_orphans - old_orphans)
    result.fixed_orphans = sorted(url for url in old_orphans - new_orphans if url in new_nodes)

    old_counts, new_counts = old.issue_counts(), new.issue_counts()
    for code in sorted(set(old_counts) | set(new_counts)):
        before, after = old_counts.get(code, 0), new_counts.get(code, 0)
        if before != after:
            result.issue_changes[code] = (before, after)

    result.summary = {
        "old_pages": len(old_nodes),
        "new_pages": len(new_nodes),
        "pages_added": len(result.new_pages),
        "pages_removed": len(result.lost_pages),
        "old_edges": len(old_edges),
        "new_edges": len(new_edges),
        "links_added": len(result.new_links),
        "links_removed": len(result.lost_links),
        "depth_changes": len(result.depth_changes),
        "status_changes": len(result.status_changes),
        "old_issues": len(old.issues),
        "new_issues": len(new.issues),
    }
    return result


def diff_sites(
    old: SiteData,
    new: SiteData,
    config: AnalysisConfig | None = None,
    top: int = 20,
) -> CrawlDiff:
    """Analyse both crawls with the same settings and compare them."""
    config = config or AnalysisConfig()
    return diff_analyses(analyze(old, config), analyze(new, config), top=top)


def format_diff(diff: CrawlDiff, limit: int = 15) -> str:
    """Plain-text rendering of a :class:`CrawlDiff`."""
    s = diff.summary
    lines = [
        "Crawl comparison",
        "================",
        f"Pages: {s.get('old_pages', 0)} -> {s.get('new_pages', 0)} "
        f"(+{len(diff.new_pages)} / -{len(diff.lost_pages)})",
        f"Links: {s.get('old_edges', 0)} -> {s.get('new_edges', 0)} "
        f"(+{len(diff.new_links)} / -{len(diff.lost_links)})",
        f"Issues: {s.get('old_issues', 0)} -> {s.get('new_issues', 0)}",
    ]

    def section(title: str, rows: list[str], total: int) -> None:
        if not rows:
            return
        lines.append("")
        lines.append(f"{title} ({total})")
        lines.extend(f"  {row}" for row in rows)
        if total > len(rows):
            lines.append(f"  ... and {total - len(rows)} more")

    section("New pages", diff.new_pages[:limit], len(diff.new_pages))
    section("Lost pages", diff.lost_pages[:limit], len(diff.lost_pages))
    section(
        "New links", [f"{s_} -> {t}" for s_, t in diff.new_links[:limit]], len(diff.new_links)
    )
    section(
        "Lost links", [f"{s_} -> {t}" for s_, t in diff.lost_links[:limit]], len(diff.lost_links)
    )
    section(
        "PageRank gainers",
        [f"{m.url}  {m.old:.5f} -> {m.new:.5f} ({m.relative:+.1%})" for m in diff.gainers[:limit]],
        len(diff.gainers),
    )
    section(
        "PageRank losers",
        [f"{m.url}  {m.old:.5f} -> {m.new:.5f} ({m.relative:+.1%})" for m in diff.losers[:limit]],
        len(diff.losers),
    )
    section(
        "Click depth changes",
        [
            f"{url}  {_depth(old)} -> {_depth(new)}"
            for url, old, new in diff.depth_changes[:limit]
        ],
        len(diff.depth_changes),
    )
    section(
        "Status changes",
        [f"{url}  {old} -> {new}" for url, old, new in diff.status_changes[:limit]],
        len(diff.status_changes),
    )
    section("New orphans", diff.new_orphans[:limit], len(diff.new_orphans))
    section("Fixed orphans", diff.fixed_orphans[:limit], len(diff.fixed_orphans))
    section(
        "Issue count changes",
        [f"{code}: {old} -> {new}" for code, (old, new) in diff.issue_changes.items()],
        len(diff.issue_changes),
    )
    if not diff.has_changes:
        lines.append("")
        lines.append("No structural changes.")
    return "\n".join(lines) + "\n"


def _depth(value: int | None) -> str:
    return "unreachable" if value is None else str(value)
