import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import pytest

from conftest import BASE, build_site
from internal_link_graph.analyzer import analyze
from internal_link_graph.crawler import crawl
from internal_link_graph.htmlreport import (
    depth_chart,
    nice_ticks,
    pagerank_chart,
    svg_bar_chart,
    to_html,
)
from internal_link_graph.suggest import Suggestion

HOME = f"{BASE}/"
SVG = "{http://www.w3.org/2000/svg}"
VOID = {"meta", "br", "hr", "img", "input", "link", "line", "path", "rect", "circle", "col"}


class Balance(HTMLParser):
    """Checks that every non-void element is closed in the right order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str, str]] = []
        self.errors: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend((tag, name, value or "") for name, value in attrs)
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend((tag, name, value or "") for name, value in attrs)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> (open: {self.stack[-3:]})")
        else:
            self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)


def parse(document: str) -> Balance:
    parser = Balance()
    parser.feed(document)
    parser.close()
    return parser


@pytest.fixture
def crawled(fetcher):
    return analyze(crawl(HOME, fetcher=fetcher, delay=0, workers=1))


@pytest.mark.parametrize(
    ("maximum", "expected"),
    [
        (0, [0.0, 1.0]),
        (4, [0.0, 1.0, 2.0, 3.0, 4.0]),
        (7, [0.0, 2.0, 4.0, 6.0, 8.0]),
        (100, [0.0, 50.0, 100.0]),
        (1, [0.0, 0.5, 1.0]),
    ],
)
def test_nice_ticks(maximum, expected):
    assert nice_ticks(maximum) == pytest.approx(expected)


@pytest.mark.parametrize("maximum", [1, 3, 9, 13, 47, 250, 999, 12345, 0.3])
def test_nice_ticks_cover_the_maximum(maximum):
    ticks = nice_ticks(maximum)
    assert ticks[0] == 0 and ticks[-1] >= maximum - 1e-9
    assert 2 <= len(ticks) <= 8
    steps = {round(b - a, 9) for a, b in zip(ticks, ticks[1:], strict=False)}
    assert len(steps) == 1


def test_svg_bar_chart_is_valid_xml_with_one_bar_per_positive_value():
    svg = svg_bar_chart([("0", 1, "one"), ("1", 4, "four"), ("2", 0, "none")], "x", "y")
    root = ET.fromstring(svg)
    assert root.tag == f"{SVG}svg" and root.get("role") == "img"
    assert root.get("viewBox") == "0 0 520 260"
    bars = root.findall(f"{SVG}path")
    assert len(bars) == 2
    assert [bar.find(f"{SVG}title").text for bar in bars] == ["one", "four"]
    texts = [text.text for text in root.findall(f"{SVG}text")]
    assert {"0", "1", "2", "x", "y", "4"} <= set(texts)


def test_svg_bars_stay_inside_the_plot_and_scale_with_value():
    svg = svg_bar_chart([("a", 2, "t"), ("b", 8, "t")], width=400, height=200)
    heights = []
    for path in ET.fromstring(svg).findall(f"{SVG}path"):
        numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", path.get("d"))]
        xs, ys = numbers[0::2], numbers[1::2]
        match = re.match(r"M([\d.]+),([\d.]+) V([\d.]+) Q[\d.]+,([\d.]+)", path.get("d"))
        left, bottom, top = float(match[1]), float(match[2]), float(match[4])
        assert 48 <= left <= 400 - 12 and 18 <= top < bottom <= 200 - 44 + 0.1
        heights.append(bottom - top)
        assert xs and ys
    assert heights[1] == pytest.approx(4 * heights[0], rel=0.02)


def test_svg_bar_chart_escapes_text_and_handles_no_data():
    svg = svg_bar_chart([("<a>", 1, 'tip & "more"')], aria_label='A "label" <x>')
    root = ET.fromstring(svg)
    assert root.get("aria-label") == 'A "label" <x>'
    assert root.find(f"{SVG}path/{SVG}title").text == 'tip & "more"'
    assert "No data" in svg_bar_chart([])


def test_svg_bar_chart_thins_labels_for_many_bars():
    bars = [(str(i), float(i + 1), f"bar {i}") for i in range(40)]
    root = ET.fromstring(svg_bar_chart(bars))
    assert len(root.findall(f"{SVG}path")) == 40
    assert not root.findall(f"{SVG}text[@class='value']")
    labels = [t for t in root.findall(f"{SVG}text") if t.get("text-anchor") == "middle"]
    assert len(labels) <= 14


def test_depth_and_pagerank_charts(crawled):
    depth = ET.fromstring(depth_chart(crawled))
    assert depth.get("aria-label") == "Pages by click depth"
    titles = [title.text for title in depth.iter(f"{SVG}title")]
    assert titles[0] == "Depth 0: 1 page" and len(titles) == len(crawled.depth_histogram())
    pagerank = ET.fromstring(pagerank_chart(crawled))
    counts = [int(re.search(r": (\d+) page", t.text)[1]) for t in pagerank.iter(f"{SVG}title")]
    assert sum(counts) == len(crawled.nodes)


def test_html_report_is_balanced_and_self_contained(crawled):
    document = to_html(crawled)
    assert document.startswith("<!DOCTYPE html>")
    parsed = parse(document)
    assert parsed.errors == [] and parsed.stack == []
    assert "script" not in parsed.tags and "link" not in parsed.tags
    assert "img" not in parsed.tags and "iframe" not in parsed.tags
    assert parsed.tags.count("svg") == 2 and parsed.tags.count("h1") == 1
    external = [value for _tag, name, value in parsed.attrs if name in ("src", "href")]
    assert external == []
    assert "@import" not in document and "url(" not in document
    assert not [name for _tag, name, _value in parsed.attrs if name.startswith("on")]


def test_html_report_contents(crawled):
    document = to_html(crawled)
    text = " ".join(parse(document).text)
    assert "Internal link report" in text and HOME in text
    assert "Pages analysed" in text and "Orphan pages" in text
    assert "Top 7 pages by PageRank" in text
    assert f"{BASE}/missing" in text and f"{BASE}/orphan" in text
    for title in {issue.title for issue in crawled.issues}:
        assert title in text
    assert '<meta charset="utf-8">' in document


def test_html_report_escapes_page_content(small_site):
    small_site.pages[f"{BASE}/a"].title = '<script>alert("x")</script> & co'
    document = to_html(analyze(small_site), title="Report <b>1</b>")
    assert "<script>" not in document and "<b>1</b>" not in document
    assert "&lt;script&gt;" in document
    parsed = parse(document)
    assert parsed.errors == [] and "script" not in parsed.tags
    assert "Report <b>1</b>" in " ".join(parsed.text)


def test_html_report_with_suggestions(small_site):
    item = Suggestion(
        source=f"{BASE}/a", target=f"{BASE}/o", score=0.5, similarity=0.42, source_pagerank=0.3,
        target_depth=None, target_inlinks=0, anchor="Anchor <text>", shared_terms=["rose"],
        reason="no internal inlinks",
    )
    result = analyze(small_site)
    document = to_html(result, [item])
    parsed = parse(document)
    assert parsed.errors == []
    assert "Anchor <text>" in " ".join(parsed.text)
    assert len(document) > len(to_html(result))


def test_html_report_limits_rows(crawled):
    short = to_html(crawled, top=2, issue_examples=1)
    assert "Top 2 pages by PageRank" in short
    assert len(short) < len(to_html(crawled))
    assert parse(short).errors == []


def test_html_report_for_empty_and_clean_sites():
    empty = to_html(analyze(build_site([])))
    assert parse(empty).errors == [] and "No data" in empty
    clean = build_site([("home", "a"), ("home", "b"), ("a", "home"), ("a", "b"), ("b", "home"),
                        ("b", "a")], start="home")
    assert "No issues found." in to_html(analyze(clean))


def test_html_report_supports_dark_mode_and_non_ascii(small_site):
    small_site.pages[f"{BASE}/a"].title = "গোলাপ গাছের যত্ন"
    document = to_html(analyze(small_site))
    assert "prefers-color-scheme: dark" in document
    assert "গোলাপ গাছের যত্ন" in document
