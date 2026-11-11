"""Human readable reports: terminal summary and Markdown."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .analyzer import ISSUE_TYPES, SEVERITIES, AnalysisResult
from .suggest import Suggestion
from .urlnorm import short_path


def format_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    align: Sequence[str] | None = None,
) -> str:
    """Render a fixed-width text table.

    ``align`` holds one of ``"l"`` / ``"r"`` per column (default: left).
    """
    cells = [[str(value) for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in cells:
        for index, value in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(value))
    align = list(align or ["l"] * len(headers))
    align += ["l"] * (len(headers) - len(align))

    def render(row: Sequence[str]) -> str:
        parts = []
        for index, width in enumerate(widths):
            value = row[index] if index < len(row) else ""
            parts.append(value.rjust(width) if align[index] == "r" else value.ljust(width))
        return "  ".join(parts).rstrip()

    lines = [render(list(headers)), render(["-" * width for width in widths])]
    lines.extend(render(row) for row in cells)
    return "\n".join(lines)


def text_bar(value: float, maximum: float, width: int = 30) -> str:
    """A proportional bar made of ``#`` characters (at least one for non-zero values)."""
    if maximum <= 0 or value <= 0:
        return ""
    return "#" * max(1, round(width * value / maximum))


def _depth_label(depth: int | None) -> str:
    return "-" if depth is None else str(depth)


def _percent(part: float, whole: float) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "0.0%"


def pagerank_buckets(scores: Sequence[float], buckets: int = 10) -> list[tuple[float, float, int]]:
    """Histogram of PageRank values relative to the uniform score ``1/N``.

    Returns ``(low, high, count)`` tuples where the bounds are multiples of
    the average PageRank.  The final bucket is open ended.
    """
    count = len(scores)
    if not count or buckets < 1:
        return []
    average = sum(scores) / count
    if average <= 0:
        return [(0.0, 0.0, count)]
    ratios = [score / average for score in scores]
    top = max(ratios)
    # Buckets of width 0.5x average, capped so a single huge homepage score
    # does not flatten everything else into one bar.
    step = 0.5
    edges = [step * index for index in range(buckets)]
    result = []
    for index, low in enumerate(edges):
        last = index == len(edges) - 1
        high = top if last else low + step
        members = sum(1 for ratio in ratios if ratio >= low and (last or ratio < low + step))
        result.append((low, max(high, low), members))
    while len(result) > 1 and result[-1][2] == 0:
        result.pop()
    return result


# -- terminal ------------------------------------------------------------------


def format_summary(
    result: AnalysisResult,
    suggestions: Sequence[Suggestion] | None = None,
    top: int = 10,
    path_width: int = 60,
) -> str:
    """Plain-text summary for the terminal."""
    s = result.summary
    lines = [
        f"Internal link graph: {result.start_url or '(no start URL)'}",
        "=" * 72,
        f"Pages analysed      {s['pages']:>8}   (URLs known: {s['urls_known']})",
        f"Indexable pages     {s['indexable_pages']:>8}",
        f"Unique link edges   {s['edges']:>8}   (internal links: {s['internal_links']}, "
        f"external: {s['external_links']})",
        f"Average inlinks     {s['average_in_degree']:>8}",
        f"Max click depth     {s['max_depth']:>8}   (average {s['average_depth']})",
        f"Orphan pages        {s['orphans']:>8}",
        f"Unreachable pages   {s['unreachable']:>8}",
        f"Dead ends           {s['dead_ends']:>8}",
        f"Link equity lost    {100 * s['equity_lost']:>7.2f}%   "
        + ", ".join(f"{kind} {100 * amount:.2f}%" for kind, amount in
                    s["equity_lost_by_kind"].items()),
        f"PageRank            {s['pagerank_iterations']:>8} iterations"
        + ("" if s["pagerank_converged"] else " (did not converge)"),
    ]
    if s.get("status_codes"):
        codes = ", ".join(f"{code}: {count}" for code, count in s["status_codes"].items())
        lines.append(f"Status codes        {codes}")

    lines += ["", f"Top {min(top, len(result.nodes))} pages by PageRank", ""]
    rows = [
        [
            index + 1,
            f"{node.pagerank:.5f}",
            node.in_degree,
            node.out_degree,
            _depth_label(node.depth),
            short_path(node.url, path_width),
        ]
        for index, node in enumerate(result.top_pages(top))
    ]
    lines.append(
        format_table(["#", "PageRank", "In", "Out", "Depth", "URL"], rows, "rrrrrl")
    )

    histogram = result.depth_histogram()
    if histogram:
        lines += ["", "Click depth distribution", ""]
        peak = max(histogram.values())
        for depth, count in histogram.items():
            lines.append(f"  {depth:>3}  {count:>6}  {text_bar(count, peak)}")
        if result.unreachable:
            lines.append(f"  n/a  {len(result.unreachable):>6}  (unreachable)")

    counts = result.issue_counts()
    if counts:
        by_severity = s["issues_by_severity"]
        lines += [
            "",
            f"Issues: {s['issues']} "
            + "("
            + ", ".join(f"{by_severity.get(name, 0)} {name}s" for name in SEVERITIES)
            + ")",
            "",
        ]
        rows = [[ISSUE_TYPES[code][0], count, ISSUE_TYPES[code][1]] for code, count in
                counts.items()]
        lines.append(format_table(["Severity", "Count", "Issue"], rows, "lrl"))
    else:
        lines += ["", "No issues found."]

    if result.orphans:
        lines += ["", f"Orphan pages ({len(result.orphans)})", ""]
        lines += [f"  {short_path(url, path_width)}" for url in result.orphans[:top]]
        if len(result.orphans) > top:
            lines.append(f"  ... and {len(result.orphans) - top} more")

    if len(result.clusters.members) > 1:
        lines += ["", "Topic clusters (by URL directory)", ""]
        rows = []
        for name in result.clusters.names[:top]:
            rows.append(
                [
                    name,
                    len(result.clusters.members[name]),
                    result.clusters.internal_links(name),
                    result.clusters.outgoing_links(name),
                    result.clusters.incoming_links(name),
                    f"{result.clusters.cohesion(name):.2f}",
                ]
            )
        lines.append(
            format_table(["Cluster", "Pages", "Internal", "Out", "In", "Cohesion"], rows, "lrrrrr")
        )

    if suggestions:
        lines += ["", f"Link suggestions ({len(suggestions)})", ""]
        for suggestion in suggestions[:top]:
            lines.append(
                f"  {short_path(suggestion.source, 40)} -> {short_path(suggestion.target, 40)}"
            )
            lines.append(
                f"      anchor: \"{suggestion.anchor}\"  similarity {suggestion.similarity:.2f}"
            )
        if len(suggestions) > top:
            lines.append(f"  ... and {len(suggestions) - top} more")
    return "\n".join(lines) + "\n"


# -- markdown ------------------------------------------------------------------


def md_escape(text: Any) -> str:
    """Escape the characters that would break a Markdown table cell."""
    value = str(text).replace("\\", "\\\\").replace("|", "\\|")
    return value.replace("\n", " ").replace("\r", " ")


def md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
             align: Sequence[str] | None = None) -> str:
    align = list(align or ["l"] * len(headers))
    align += ["l"] * (len(headers) - len(align))
    marks = ["---:" if value == "r" else "---" for value in align]
    lines = [
        "| " + " | ".join(md_escape(header) for header in headers) + " |",
        "| " + " | ".join(marks) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(md_escape(value) for value in row) + " |")
    return "\n".join(lines)


def to_markdown(
    result: AnalysisResult,
    suggestions: Sequence[Suggestion] | None = None,
    top: int = 20,
    issue_examples: int = 10,
) -> str:
    """Markdown report suitable for a ticket, wiki page or pull request."""
    s = result.summary
    out: list[str] = [
        f"# Internal link report: {result.start_url or 'site'}",
        "",
        "## Summary",
        "",
        md_table(
            ["Metric", "Value"],
            [
                ["Pages analysed", s["pages"]],
                ["Indexable pages", s["indexable_pages"]],
                ["Unique link edges", s["edges"]],
                ["Internal links", s["internal_links"]],
                ["External links", s["external_links"]],
                ["Average inlinks per page", s["average_in_degree"]],
                ["Max click depth", s["max_depth"]],
                ["Average click depth", s["average_depth"]],
                ["Orphan pages", s["orphans"]],
                ["Unreachable pages", s["unreachable"]],
                ["Dead ends", s["dead_ends"]],
                ["Link equity lost", f"{100 * s['equity_lost']:.2f}%"],
                ["Issues", s["issues"]],
            ],
            "lr",
        ),
        "",
        f"## Top {min(top, len(result.nodes))} pages by PageRank",
        "",
        md_table(
            ["#", "URL", "PageRank", "Inlinks", "Outlinks", "Depth"],
            [
                [
                    index + 1,
                    node.url,
                    f"{node.pagerank:.5f}",
                    node.in_degree,
                    node.out_degree,
                    _depth_label(node.depth),
                ]
                for index, node in enumerate(result.top_pages(top))
            ],
            "rlrrrr",
        ),
        "",
    ]

    histogram = result.depth_histogram()
    if histogram:
        total = sum(histogram.values())
        out += [
            "## Click depth",
            "",
            md_table(
                ["Depth", "Pages", "Share"],
                [[depth, count, _percent(count, total)] for depth, count in histogram.items()],
                "rrr",
            ),
            "",
        ]

    out += ["## Issues", ""]
    grouped = result.issues_by_code()
    if not grouped:
        out += ["No issues found.", ""]
    for code in ISSUE_TYPES:
        issues = grouped.get(code)
        if not issues:
            continue
        severity, title = ISSUE_TYPES[code]
        out += [f"### {title} ({len(issues)}, {severity})", ""]
        for issue in issues[:issue_examples]:
            line = f"- `{issue.url}`"
            if issue.target:
                line += f" -> `{issue.target}`"
            if issue.detail:
                line += f": {issue.detail}"
            out.append(line)
        if len(issues) > issue_examples:
            out.append(f"- ... and {len(issues) - issue_examples} more")
        out.append("")

    if result.equity.losses:
        out += [
            "## Link equity lost",
            "",
            md_table(
                ["Kind", "Links", "Share of total PageRank"],
                [
                    [kind, result.equity.counts.get(kind, 0), f"{100 * amount:.3f}%"]
                    for kind, amount in result.equity.totals.items()
                ],
                "lrr",
            ),
            "",
            md_table(
                ["Source", "Target", "Kind", "Lost"],
                [
                    [loss.source, loss.target, loss.kind, f"{100 * loss.amount:.3f}%"]
                    for loss in result.equity.top(top)
                ],
                "lllr",
            ),
            "",
        ]

    profiles = sorted(result.anchors.values(), key=lambda p: (-p.total, p.target))[:top]
    if profiles:
        out += [
            "## Anchor text",
            "",
            md_table(
                ["Target", "Links", "Unique anchors", "Top anchor", "Share", "Generic", "Empty"],
                [
                    [
                        profile.target,
                        profile.total,
                        profile.unique,
                        profile.top_anchor,
                        f"{100 * profile.top_share:.0f}%",
                        profile.generic,
                        profile.empty,
                    ]
                    for profile in profiles
                ],
                "lrrlrrr",
            ),
            "",
        ]

    if len(result.clusters.members) > 1:
        names = result.clusters.names
        out += [
            "## Topic clusters",
            "",
            md_table(
                ["Cluster", "Pages", "Internal links", "Outgoing", "Incoming", "Cohesion"],
                [
                    [
                        name,
                        len(result.clusters.members[name]),
                        result.clusters.internal_links(name),
                        result.clusters.outgoing_links(name),
                        result.clusters.incoming_links(name),
                        f"{result.clusters.cohesion(name):.2f}",
                    ]
                    for name in names
                ],
                "lrrrrr",
            ),
            "",
        ]
        if len(names) <= 12:
            out += [
                "Links from row cluster to column cluster:",
                "",
                md_table(
                    ["from / to", *names],
                    [
                        [name, *[result.clusters.links_between(name, other) for other in names]]
                        for name in names
                    ],
                    "l" + "r" * len(names),
                ),
                "",
            ]

    if result.duplicate_titles:
        out += ["## Duplicate titles", ""]
        for cluster in result.duplicate_titles[:top]:
            kind = "exact" if cluster.exact else "near"
            out.append(f"- **{md_escape(cluster.titles[0])}** ({kind}, {cluster.size} pages)")
            out += [f"  - `{url}`" for url in cluster.urls[:issue_examples]]
        out.append("")

    if suggestions:
        out += [
            "## Link suggestions",
            "",
            md_table(
                ["Link from", "Link to", "Suggested anchor", "Similarity", "Why"],
                [
                    [
                        suggestion.source,
                        suggestion.target,
                        suggestion.anchor,
                        f"{suggestion.similarity:.2f}",
                        suggestion.reason,
                    ]
                    for suggestion in suggestions[:top]
                ],
                "lllrl",
            ),
            "",
        ]
    return "\n".join(out).rstrip() + "\n"
