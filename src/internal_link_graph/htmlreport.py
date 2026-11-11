"""Self-contained HTML report with inline SVG charts.

The generated document has no external dependencies: styles are inlined, the
charts are plain SVG and there is no JavaScript, so the file can be e-mailed,
attached to a ticket or opened from disk.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from html import escape
from typing import Any

from . import __version__
from .analyzer import ISSUE_TYPES, AnalysisResult
from .report import pagerank_buckets
from .suggest import Suggestion

_CSS = """
:root {
  --surface: #fcfcfb; --panel: #ffffff; --border: #e4e3de; --grid: #ebeae5;
  --text: #0b0b0b; --text-2: #52514e; --series: #2a78d6;
  --error: #b3261e; --warning: #8a5a00; --notice: #52514e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface: #1a1a19; --panel: #222221; --border: #3a3a37; --grid: #30302e;
    --text: #f0efec; --text-2: #c3c2b7; --series: #3987e5;
    --error: #f2857d; --warning: #e0b050; --notice: #c3c2b7;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 24px 64px; background: var(--surface); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 40px 0 12px; }
h3 { font-size: 15px; margin: 0 0 8px; }
p.meta, .muted { color: var(--text-2); }
p.meta { margin: 0 0 24px; word-break: break-all; }
a { color: inherit; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.tile { background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 14px; }
.tile .value { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .label { color: var(--text-2); font-size: 13px; }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 16px; }
figure { margin: 0; background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px; }
figcaption { font-weight: 600; margin-bottom: 4px; }
figure p { margin: 0 0 8px; color: var(--text-2); font-size: 13px; }
svg { display: block; width: 100%; height: auto; }
svg .bar { fill: var(--series); }
svg .bar:hover { opacity: 0.8; }
svg .grid { stroke: var(--grid); stroke-width: 1; }
svg .axis { stroke: var(--border); stroke-width: 1; }
svg text { fill: var(--text-2); font-size: 11px; font-family: inherit; }
svg text.value { fill: var(--text); }
.table-wrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid var(--border);
  vertical-align: top; }
tr:last-child td { border-bottom: 0; }
th { color: var(--text-2); font-weight: 600; white-space: nowrap; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.url { word-break: break-all; min-width: 220px; }
details { margin: 8px 0; }
summary { cursor: pointer; padding: 6px 0; }
.sev { font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.04em; }
.sev-error { color: var(--error); }
.sev-warning { color: var(--warning); }
.sev-notice { color: var(--notice); }
footer { margin-top: 48px; color: var(--text-2); font-size: 12px; }
"""


def nice_ticks(maximum: float, target: int = 4) -> list[float]:
    """Round axis tick values covering ``0..maximum`` (always includes 0)."""
    if maximum <= 0:
        return [0.0, 1.0]
    raw = maximum / max(target, 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    step = magnitude
    for factor in (1, 2, 5, 10):
        step = factor * magnitude
        if raw <= step:
            break
    if maximum >= target and step < 1:
        step = 1.0
    ticks = [0.0]
    while ticks[-1] < maximum - 1e-9:
        ticks.append(round(ticks[-1] + step, 10))
    return ticks


def _number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{round(value):,}"
    return f"{value:g}"


def _bar_path(x: float, y: float, width: float, height: float, radius: float) -> str:
    """Bar anchored on the baseline with rounded top corners."""
    radius = max(0.0, min(radius, width / 2, height))
    bottom = y + height
    return (
        f"M{x:.1f},{bottom:.1f} V{y + radius:.1f} "
        f"Q{x:.1f},{y:.1f} {x + radius:.1f},{y:.1f} "
        f"H{x + width - radius:.1f} "
        f"Q{x + width:.1f},{y:.1f} {x + width:.1f},{y + radius:.1f} "
        f"V{bottom:.1f} Z"
    )


def svg_bar_chart(
    bars: Sequence[tuple[str, float, str]],
    x_label: str = "",
    y_label: str = "",
    width: int = 520,
    height: int = 260,
    aria_label: str = "Bar chart",
) -> str:
    """Render ``(label, value, tooltip)`` bars as an inline SVG histogram."""
    if not bars:
        return '<p class="muted">No data.</p>'
    left, right, top, bottom = 48, 12, 18, 44
    plot_w = width - left - right
    plot_h = height - top - bottom
    ticks = nice_ticks(max(value for _, value, _ in bars))
    scale_max = ticks[-1] or 1.0
    slot = plot_w / len(bars)
    gap = 2.0 if slot > 6 else 0.5
    bar_w = min(max(slot - gap, 1.0), 56.0)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(aria_label, True)}" '
        'xmlns="http://www.w3.org/2000/svg">'
    ]
    for tick in ticks:
        y = top + plot_h - plot_h * tick / scale_max
        css = "axis" if tick == 0 else "grid"
        parts.append(
            f'<line class="{css}" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text x="{left - 6}" y="{y + 3.5:.1f}" text-anchor="end">{_number(tick)}</text>'
        )
    label_every = max(1, math.ceil(len(bars) / 12))
    show_values = len(bars) <= 14
    for index, (label, value, tooltip) in enumerate(bars):
        bar_h = plot_h * value / scale_max
        if value > 0:
            bar_h = max(bar_h, 1.5)
        x = left + slot * index + (slot - bar_w) / 2
        y = top + plot_h - bar_h
        centre = x + bar_w / 2
        if value > 0:
            parts.append(
                f'<path class="bar" d="{_bar_path(x, y, bar_w, bar_h, 4)}">'
                f"<title>{escape(tooltip)}</title></path>"
            )
            if show_values:
                parts.append(
                    f'<text class="value" x="{centre:.1f}" y="{y - 4:.1f}" '
                    f'text-anchor="middle">{_number(value)}</text>'
                )
        if index % label_every == 0:
            parts.append(
                f'<text x="{centre:.1f}" y="{top + plot_h + 15}" text-anchor="middle">'
                f"{escape(label)}</text>"
            )
    if x_label:
        parts.append(
            f'<text x="{left + plot_w / 2:.1f}" y="{height - 8}" text-anchor="middle">'
            f"{escape(x_label)}</text>"
        )
    if y_label:
        parts.append(f'<text x="4" y="11">{escape(y_label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def depth_chart(result: AnalysisResult) -> str:
    histogram = result.depth_histogram()
    bars = [
        (str(depth), float(count), f"Depth {depth}: {count} page{'s' if count != 1 else ''}")
        for depth, count in histogram.items()
    ]
    return svg_bar_chart(
        bars,
        x_label="Clicks from the start URL",
        y_label="Pages",
        aria_label="Pages by click depth",
    )


def pagerank_chart(result: AnalysisResult) -> str:
    buckets = pagerank_buckets([node.pagerank for node in result.nodes.values()])
    bars = []
    for index, (low, high, count) in enumerate(buckets):
        last = index == len(buckets) - 1
        label = f"{low:g}x+" if last and len(buckets) > 1 else f"{low:g}x"
        span = f"{low:g}x or more" if last and len(buckets) > 1 else f"{low:g}x to {high:g}x"
        bars.append(
            (label, float(count), f"{span} the average PageRank: {count} "
             f"page{'s' if count != 1 else ''}")
        )
    return svg_bar_chart(
        bars,
        x_label="PageRank relative to the site average",
        y_label="Pages",
        aria_label="Distribution of PageRank",
    )


def _table(headers: Sequence[tuple[str, str]], rows: Sequence[Sequence[Any]]) -> str:
    """``headers`` is a list of ``(title, css_class)``; cells are escaped."""
    if not rows:
        return '<p class="muted">Nothing to show.</p>'
    head = "".join(
        f'<th class="{css}">{escape(title)}</th>' if css else f"<th>{escape(title)}</th>"
        for title, css in headers
    )
    body = []
    for row in rows:
        cells = []
        for (_, css), value in zip(headers, row, strict=False):
            text = escape("" if value is None else str(value))
            cells.append(f'<td class="{css}">{text}</td>' if css else f"<td>{text}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


def _tile(label: str, value: Any) -> str:
    return (
        f'<div class="tile"><div class="value">{escape(str(value))}</div>'
        f'<div class="label">{escape(label)}</div></div>'
    )


def _depth(value: int | None) -> str:
    return "-" if value is None else str(value)


def to_html(
    result: AnalysisResult,
    suggestions: Sequence[Suggestion] | None = None,
    top: int = 25,
    issue_examples: int = 25,
    title: str | None = None,
) -> str:
    """Build the complete HTML document."""
    s = result.summary
    heading = title or "Internal link report"
    out: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(heading)}</title>",
        f"<style>{_CSS}</style>",
        "</head><body><main>",
        f"<h1>{escape(heading)}</h1>",
        f'<p class="meta">{escape(result.start_url or "")}</p>',
        '<section class="tiles">',
        _tile("Pages analysed", f"{s['pages']:,}"),
        _tile("Unique link edges", f"{s['edges']:,}"),
        _tile("Average inlinks", s["average_in_degree"]),
        _tile("Max click depth", s["max_depth"]),
        _tile("Orphan pages", f"{s['orphans']:,}"),
        _tile("Dead ends", f"{s['dead_ends']:,}"),
        _tile("Link equity lost", f"{100 * s['equity_lost']:.1f}%"),
        _tile("Issues", f"{s['issues']:,}"),
        "</section>",
        "<h2>Distributions</h2>",
        '<section class="charts">',
        "<figure><figcaption>Click depth</figcaption>"
        "<p>How many clicks each page is away from the start URL.</p>",
        depth_chart(result),
        "</figure>",
        "<figure><figcaption>PageRank distribution</figcaption>"
        "<p>Pages grouped by internal PageRank relative to the site average (1x).</p>",
        pagerank_chart(result),
        "</figure>",
        "</section>",
    ]

    histogram = result.depth_histogram()
    total = sum(histogram.values())
    out.append("<details><summary>Click depth as a table</summary>")
    out.append(
        _table(
            [("Depth", "num"), ("Pages", "num"), ("Share", "num")],
            [
                [depth, count, f"{100 * count / total:.1f}%" if total else "0%"]
                for depth, count in histogram.items()
            ],
        )
    )
    out.append("</details>")

    out.append(f"<h2>Top {min(top, len(result.nodes))} pages by PageRank</h2>")
    out.append(
        _table(
            [
                ("#", "num"),
                ("URL", "url"),
                ("Title", ""),
                ("PageRank", "num"),
                ("Inlinks", "num"),
                ("Outlinks", "num"),
                ("Depth", "num"),
            ],
            [
                [
                    index + 1,
                    node.url,
                    node.title,
                    f"{node.pagerank:.5f}",
                    node.in_degree,
                    node.out_degree,
                    _depth(node.depth),
                ]
                for index, node in enumerate(result.top_pages(top))
            ],
        )
    )

    out.append("<h2>Issues</h2>")
    grouped = result.issues_by_code()
    if not grouped:
        out.append('<p class="muted">No issues found.</p>')
    for code, (severity, issue_title) in ISSUE_TYPES.items():
        issues = grouped.get(code)
        if not issues:
            continue
        out.append(
            f'<details><summary><span class="sev sev-{severity}">{severity}</span> '
            f"{escape(issue_title)} ({len(issues):,})</summary>"
        )
        out.append(
            _table(
                [("URL", "url"), ("Target", "url"), ("Detail", "")],
                [[issue.url, issue.target, issue.detail] for issue in issues[:issue_examples]],
            )
        )
        if len(issues) > issue_examples:
            out.append(f'<p class="muted">... and {len(issues) - issue_examples:,} more.</p>')
        out.append("</details>")

    if result.equity.losses:
        out.append("<h2>Link equity lost</h2>")
        out.append(
            _table(
                [("Source", "url"), ("Target", "url"), ("Kind", ""), ("Lost", "num")],
                [
                    [loss.source, loss.target, loss.kind, f"{100 * loss.amount:.3f}%"]
                    for loss in result.equity.top(top)
                ],
            )
        )

    profiles = sorted(result.anchors.values(), key=lambda p: (-p.total, p.target))[:top]
    if profiles:
        out.append("<h2>Anchor text</h2>")
        out.append(
            _table(
                [
                    ("Target", "url"),
                    ("Links", "num"),
                    ("Unique", "num"),
                    ("Top anchor", ""),
                    ("Share", "num"),
                    ("Generic", "num"),
                    ("Empty", "num"),
                ],
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
            )
        )

    if len(result.clusters.members) > 1:
        clusters = result.clusters
        out.append("<h2>Topic clusters</h2>")
        out.append(
            _table(
                [
                    ("Cluster", ""),
                    ("Pages", "num"),
                    ("Internal links", "num"),
                    ("Outgoing", "num"),
                    ("Incoming", "num"),
                    ("Cohesion", "num"),
                ],
                [
                    [
                        name,
                        len(clusters.members[name]),
                        clusters.internal_links(name),
                        clusters.outgoing_links(name),
                        clusters.incoming_links(name),
                        f"{clusters.cohesion(name):.2f}",
                    ]
                    for name in clusters.names
                ],
            )
        )
        names = clusters.names
        if len(names) <= 15:
            out.append("<details><summary>Cross-cluster link matrix</summary>")
            out.append(
                _table(
                    [("from / to", ""), *[(name, "num") for name in names]],
                    [
                        [name, *[clusters.links_between(name, other) for other in names]]
                        for name in names
                    ],
                )
            )
            out.append("</details>")

    if result.duplicate_titles:
        out.append("<h2>Duplicate titles</h2>")
        out.append(
            _table(
                [("Title", ""), ("Match", ""), ("Pages", "num"), ("URLs", "url")],
                [
                    [
                        cluster.titles[0],
                        "exact" if cluster.exact else "near",
                        cluster.size,
                        ", ".join(cluster.urls[:5]),
                    ]
                    for cluster in result.duplicate_titles[:top]
                ],
            )
        )

    if suggestions:
        out.append("<h2>Link suggestions</h2>")
        out.append(
            _table(
                [
                    ("Link from", "url"),
                    ("Link to", "url"),
                    ("Suggested anchor", ""),
                    ("Similarity", "num"),
                    ("Why", ""),
                ],
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
            )
        )

    out.append(f"<footer>internal-link-graph {escape(__version__)}</footer>")
    out.append("</main></body></html>")
    return "\n".join(out) + "\n"
