import pytest

from conftest import BASE, build_site
from internal_link_graph.analyzer import analyze
from internal_link_graph.crawler import crawl
from internal_link_graph.report import (
    format_summary,
    format_table,
    md_escape,
    md_table,
    pagerank_buckets,
    text_bar,
    to_markdown,
)
from internal_link_graph.suggest import Suggestion

HOME = f"{BASE}/"


def u(name: str) -> str:
    return f"{BASE}/{name}"


def suggestion(index: int = 0) -> Suggestion:
    return Suggestion(
        source=u("a"), target=u(f"t{index}"), score=0.5, similarity=0.42, source_pagerank=0.3,
        target_depth=4, target_inlinks=1, anchor="Target | page", shared_terms=["rose"],
        reason="4 clicks deep",
    )


def test_format_table_alignment():
    table = format_table(["Name", "Count"], [["a", 5], ["longer", 123]], align="lr")
    lines = table.splitlines()
    assert lines[0] == "Name    Count"
    assert lines[1] == "------  -----"
    assert lines[2] == "a           5"
    assert lines[3] == "longer    123"


def test_format_table_handles_short_rows_and_no_trailing_spaces():
    table = format_table(["A", "B", "C"], [["x"], ["y", "z", "w"]])
    assert all(line == line.rstrip() for line in table.splitlines())
    assert table.splitlines()[2] == "x"


@pytest.mark.parametrize(
    ("value", "maximum", "expected"),
    [(10, 10, 30), (5, 10, 15), (0.001, 10, 1), (0, 10, 0), (5, 0, 0), (-1, 10, 0)],
)
def test_text_bar(value, maximum, expected):
    assert text_bar(value, maximum) == "#" * expected


def test_text_bar_width():
    assert text_bar(1, 2, width=10) == "#####"


def test_pagerank_buckets_count_every_page_once():
    scores = [0.4, 0.2, 0.1, 0.1, 0.1, 0.05, 0.05]
    buckets = pagerank_buckets(scores)
    assert sum(count for _, _, count in buckets) == len(scores)
    assert buckets[0][0] == 0.0 and buckets[0][1] == 0.5
    assert all(low <= high for low, high, _ in buckets)
    assert buckets[-1][2] > 0


def test_pagerank_buckets_open_ended_last_bucket():
    scores = [0.91] + [0.01] * 9
    buckets = pagerank_buckets(scores, buckets=4)
    assert len(buckets) == 4
    assert buckets[-1][2] == 1 and buckets[-1][1] == pytest.approx(9.1)
    assert sum(count for _, _, count in buckets) == 10


def test_pagerank_buckets_edge_cases():
    assert pagerank_buckets([]) == []
    assert pagerank_buckets([0.5], buckets=0) == []
    assert pagerank_buckets([0.0, 0.0]) == [(0.0, 0.0, 2)]
    uniform = pagerank_buckets([0.25] * 4)
    assert uniform[-1] == (1.0, 1.5, 4) and sum(count for _, _, count in uniform) == 4


def test_format_summary_contents(small_site):
    result = analyze(small_site)
    text = format_summary(result)
    assert text.startswith(f"Internal link graph: {HOME}\n")
    assert "Pages analysed             5" in text
    assert "Orphan pages               1" in text
    assert "Top 5 pages by PageRank" in text
    assert "did not converge" not in text
    assert "/a" in text and text.endswith("\n")
    lines = text.splitlines()
    first_row = next(line for line in lines if line.lstrip().startswith("1 "))
    assert first_row.rstrip().endswith("/a")


def test_format_summary_lists_issues_and_respects_top(fetcher):
    result = analyze(crawl(HOME, fetcher=fetcher, delay=0, workers=1))
    text = format_summary(result, top=3)
    assert "Top 3 pages by PageRank" in text
    assert "Broken internal links" in text or "broken" in text.lower()
    assert "Status codes" in text and "2xx: 7" in text
    for issue_code_title in {issue.title for issue in result.issues}:
        assert issue_code_title in text


def test_format_summary_with_suggestions(small_site):
    result = analyze(small_site)
    text = format_summary(result, [suggestion(i) for i in range(4)], top=2)
    assert "/t0" in text and "/t1" in text
    assert "... and 2 more" in text
    assert "/t3" not in text


def test_format_summary_for_empty_site():
    text = format_summary(analyze(build_site([])))
    assert "(no start URL)" in text and "Top 0 pages" in text


def test_md_escape():
    assert md_escape("a|b") == "a\\|b"
    assert md_escape("line\nbreak\r") == "line break "
    assert md_escape("back\\slash") == "back\\\\slash"
    assert md_escape(12) == "12"


def test_md_table():
    table = md_table(["Name", "N"], [["a|b", 1], ["c", 22]], "lr")
    assert table.splitlines() == [
        "| Name | N |",
        "| --- | ---: |",
        "| a\\|b | 1 |",
        "| c | 22 |",
    ]
    assert md_table(["A", "B"], []).count("\n") == 1


def test_markdown_report_structure(fetcher):
    result = analyze(crawl(HOME, fetcher=fetcher, delay=0, workers=1))
    text = to_markdown(result)
    assert text.startswith(f"# Internal link report: {HOME}\n")
    for heading in ("## Summary", "## Top 7 pages by PageRank", "## Click depth", "## Issues"):
        assert heading in text
    assert "| Pages analysed | 7 |" in text
    assert "(1, error)" in text
    assert f"{BASE}/missing" in text and f"{BASE}/orphan" in text
    for line in text.splitlines():
        if line.startswith("|"):
            assert line.endswith("|")


def test_markdown_tables_have_consistent_column_counts(small_site):
    text = to_markdown(analyze(small_site), [suggestion()])
    blocks, current = [], []
    for line in text.splitlines():
        if line.startswith("|"):
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    assert len(blocks) >= 3
    for block in blocks:
        counts = {line.replace("\\|", "").count("|") for line in block}
        assert len(counts) == 1, block
    assert "Target \\| page" in text


def test_markdown_examples_are_limited(small_site):
    edges = [("home", f"p{i}") for i in range(15)]
    result = analyze(build_site(edges, start="home"))
    text = to_markdown(result, issue_examples=3)
    assert "more" in text
    assert text.count(f"{BASE}/p") < to_markdown(result, issue_examples=50).count(f"{BASE}/p")


def test_markdown_without_issues():
    site = build_site([("home", "a"), ("home", "b"), ("a", "home"), ("a", "b"), ("b", "home"),
                       ("b", "a")], start="home")
    text = to_markdown(analyze(site))
    assert "No issues found." in text
