import csv
import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from conftest import BASE, build_site
from internal_link_graph import __version__
from internal_link_graph.cli import (
    EXIT_ERROR,
    EXIT_ISSUES,
    EXIT_OK,
    EXIT_USAGE,
    EXPORT_FORMATS,
    build_parser,
    format_suggestions,
    main,
)
from internal_link_graph.models import SiteData
from internal_link_graph.suggest import Suggestion

HOME = f"{BASE}/"
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample-edges.csv"


def run(*argv, fetcher=None):
    out, err = io.StringIO(), io.StringIO()
    code = main([str(arg) for arg in argv], fetcher=fetcher, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def crawl_file(tmp_path, fetcher):
    path = tmp_path / "crawl.json"
    code, _out, _err = run("crawl", HOME, "-o", path, "--delay", "0", "--workers", "1", "-q",
                           fetcher=fetcher)
    assert code == EXIT_OK
    return path


@pytest.fixture
def clean_file(tmp_path):
    site = build_site([("home", "a"), ("home", "b"), ("a", "home"), ("a", "b"), ("b", "home"),
                       ("b", "a")], start="home")
    path = tmp_path / "clean.json"
    site.save(path)
    return path


def test_no_command_prints_help():
    code, out, _err = run()
    assert code == EXIT_USAGE and "usage: internal-link-graph" in out
    for command in ("crawl", "analyze", "suggest", "export", "diff"):
        assert command in out


def test_version(capsys):
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_unknown_command_and_bad_option_values(capsys):
    assert main(["frobnicate"]) == EXIT_USAGE
    assert main(["analyze", "x.json", "--damping", "1.5"]) == EXIT_USAGE
    assert main(["crawl", HOME, "--max-pages", "0"]) == EXIT_USAGE
    assert main(["crawl", HOME, "--include", "("]) == EXIT_USAGE
    assert main(["export", "x.json"]) == EXIT_USAGE  # --to is required
    assert "usage:" in capsys.readouterr().err


def test_parser_defaults():
    args = build_parser().parse_args(["crawl", HOME])
    assert (args.output, args.max_pages, args.workers, args.delay) == ("crawl.json", 500, 4, 0.5)
    assert args.trailing_slash == "keep" and not args.ignore_robots
    args = build_parser().parse_args(["analyze", "in.json"])
    assert (args.damping, args.max_outlinks, args.deep_threshold, args.top) == (0.85, 100, 4, 10)
    assert EXPORT_FORMATS == ("json", "csv", "graphml", "dot", "markdown", "html")


def test_crawl_writes_a_loadable_file(tmp_path, fetcher):
    path = tmp_path / "nested" / "site.json"
    code, out, err = run("crawl", HOME, "-o", path, "--delay", "0", "--workers", "1",
                         fetcher=fetcher)
    assert code == EXIT_OK
    assert "Crawled 8 pages (1 redirects, 1 errors, 0 blocked by robots.txt)" in out
    assert "Found 12 internal links" in out and f"Saved {path}" in out
    assert f"links=4 {HOME}\n" in err and "[sitemap]" in err
    site = SiteData.load(path)
    assert site.start_url == HOME and len(site.pages) == 9
    assert site.pages[f"{BASE}/about"].text


def test_crawl_quiet_no_text_and_limits(tmp_path, fetcher):
    path = tmp_path / "small.json"
    code, out, err = run("crawl", HOME, "-o", path, "--delay", "0", "--workers", "1", "-q",
                         "--no-text", "--max-pages", "2", "--no-sitemap", fetcher=fetcher)
    assert code == EXIT_OK and err == ""
    assert "Crawled 2 pages" in out and "were not crawled (limit reached)" in out
    data = json.loads(path.read_text(encoding="utf-8"))
    assert all("text" not in page for page in data["pages"])


def test_crawl_exclude_and_ignore_robots(tmp_path, site_map):
    from internal_link_graph.fetcher import DictFetcher

    site_map[f"{BASE}/robots.txt"] = (200, {"Content-Type": "text/plain"},
                                      "User-agent: *\nDisallow: /blog/\n")
    path = tmp_path / "c.json"
    code, out, _err = run("crawl", HOME, "-o", path, "--delay", "0", "-q",
                          fetcher=DictFetcher(site_map))
    assert code == EXIT_OK and "1 blocked by robots.txt" in out
    code, out, _err = run("crawl", HOME, "-o", path, "--delay", "0", "-q", "--ignore-robots",
                          "--exclude", "/blog/.", fetcher=DictFetcher(site_map))
    assert "0 blocked by robots.txt" in out
    site = SiteData.load(path)
    assert site.pages[f"{BASE}/blog/"].crawled
    assert not site.pages[f"{BASE}/blog/pruning-roses"].crawled


def test_crawl_errors(tmp_path, fetcher):
    code, _out, err = run("crawl", HOME, "--resume", "-o", tmp_path / "x.json", fetcher=fetcher)
    assert code == EXIT_ERROR and "--resume needs --state" in err
    code, _out, err = run("crawl", "ftp://example.com/", "-o", tmp_path / "x.json",
                          fetcher=fetcher)
    assert code == EXIT_ERROR and err.startswith("error: ")
    assert not (tmp_path / "x.json").exists()


def test_crawl_with_state_file_can_resume(tmp_path, fetcher):
    state = tmp_path / "state.sqlite"
    out_path = tmp_path / "c.json"
    common = ("crawl", HOME, "-o", out_path, "--delay", "0", "--workers", "1", "-q",
              "--state", state)
    code, out, _err = run(*common, "--max-pages", "3", fetcher=fetcher)
    assert code == EXIT_OK and "Crawled 3 pages" in out and state.exists()
    code, _out, _err = run(*common, "--resume", fetcher=fetcher)
    assert code == EXIT_OK
    site = SiteData.load(out_path)
    assert sum(1 for page in site.pages.values() if page.status == 200) == 7


def test_analyze_prints_summary(crawl_file):
    code, out, err = run("analyze", crawl_file)
    assert code == EXIT_OK and err == ""
    assert out.startswith(f"Internal link graph: {HOME}")
    assert "Orphan pages               1" in out


def test_analyze_writes_reports(crawl_file, tmp_path):
    json_path, md_path, html_path = (tmp_path / "r" / name for name in ("a.json", "a.md", "a.html"))
    code, _out, err = run("analyze", crawl_file, "--json", json_path, "--markdown", md_path,
                          "--html", html_path, "--suggestions", "--personalized", "--top", "3")
    assert code == EXIT_OK
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["summary"]["pages"] == 7 and "suggestions" in data
    assert data["nodes"][0]["personalized_pagerank"] is not None
    assert md_path.read_text(encoding="utf-8").startswith("# Internal link report")
    assert html_path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    assert err.count("Wrote ") == 3


def test_analyze_fail_on(crawl_file, clean_file):
    assert run("analyze", crawl_file, "--fail-on", "error")[0] == EXIT_ISSUES
    assert run("analyze", crawl_file, "--fail-on", "warning")[0] == EXIT_ISSUES
    assert run("analyze", clean_file, "--fail-on", "warning")[0] == EXIT_OK
    assert run("analyze", crawl_file)[0] == EXIT_OK


def test_fail_on_error_ignores_warnings(tmp_path):
    site = build_site([("home", "a"), ("a", "home"), ("home", "b")], start="home")
    path = tmp_path / "warn.json"
    site.save(path)
    assert run("analyze", path, "--fail-on", "error")[0] == EXIT_OK
    assert run("analyze", path, "--fail-on", "warning")[0] == EXIT_ISSUES


def test_analyze_example_edge_list():
    code, out, _err = run("analyze", EXAMPLE)
    assert code == EXIT_OK
    assert "Pages analysed            14" in out and "Orphan pages               1" in out


def test_analyze_options_change_the_result(tmp_path):
    site = build_site([("home", "a"), ("a", "home"), ("home", "b")], start="home")
    site.links[2].nofollow = True
    path = tmp_path / "nf.json"
    site.save(path)
    assert "Orphan pages               1" in run("analyze", path)[1]
    assert "Orphan pages               0" in run("analyze", path, "--include-nofollow")[1]
    code, out, _err = run("analyze", path, "--start-url", f"{BASE}/a")
    assert code == EXIT_OK and out.startswith(f"Internal link graph: {BASE}/a")


def test_input_errors(tmp_path):
    code, _out, err = run("analyze", tmp_path / "missing.json")
    assert code == EXIT_ERROR and "input file not found" in err
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code, _out, err = run("analyze", bad)
    assert code == EXIT_ERROR and "cannot read" in err
    empty = tmp_path / "empty.json"
    empty.write_text('{"format": "internal-link-graph/crawl", "pages": [], "links": []}',
                     encoding="utf-8")
    code, _out, err = run("analyze", empty)
    assert code == EXIT_ERROR and "does not contain any pages" in err
    garbage = tmp_path / "garbage.csv"
    garbage.write_text("foo,bar\n1,2\n", encoding="utf-8")
    assert run("analyze", garbage)[0] == EXIT_ERROR


def test_suggest_text_json_and_csv(crawl_file, tmp_path):
    code, out, _err = run("suggest", crawl_file, "--min-similarity", "0.01")
    assert code == EXIT_OK and "link suggestion" in out
    code, out, _err = run("suggest", crawl_file, "--min-similarity", "0.01",
                          "--output-format", "json")
    payload = json.loads(out)
    assert code == EXIT_OK and payload and {"source", "target", "anchor"} <= set(payload[0])
    target = tmp_path / "s.csv"
    code, out, err = run("suggest", crawl_file, "--min-similarity", "0.01",
                         "--output-format", "csv", "-o", target)
    assert code == EXIT_OK and out == "" and f"suggestions to {target}" in err
    with target.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(payload) and rows[0]["source"] == payload[0]["source"]
    code, out, _err = run("suggest", crawl_file, "--min-similarity", "0.01",
                          "--output-format", "csv")
    assert out.startswith("source,target,anchor")


def test_suggest_limits_and_empty_result(crawl_file):
    code, out, _err = run("suggest", crawl_file, "--min-similarity", "0.01",
                          "--max-suggestions", "1", "--output-format", "json")
    assert code == EXIT_OK and len(json.loads(out)) == 1
    code, out, _err = run("suggest", crawl_file, "--min-similarity", "1")
    assert code == EXIT_OK and out.startswith("No link suggestions")


def test_format_suggestions():
    item = Suggestion(
        source=f"{BASE}/a", target=f"{BASE}/b", score=0.5, similarity=0.4217,
        source_pagerank=0.3, target_depth=4, target_inlinks=1, anchor="Anchor text",
        shared_terms=["rose", "pruning"], reason="4 clicks deep",
    )
    text = format_suggestions([item])
    assert text.startswith("1 link suggestion\n")
    assert f"1. Link from {BASE}/a" in text and '"Anchor text"' in text
    assert "similarity 0.42; 4 clicks deep" in text and "rose, pruning" in text
    assert format_suggestions([item, item]).startswith("2 link suggestions")


@pytest.mark.parametrize("fmt", ["json", "graphml", "dot", "markdown", "html"])
def test_export_to_stdout_and_file(crawl_file, tmp_path, fmt):
    code, out, err = run("export", crawl_file, "--to", fmt)
    assert code == EXIT_OK and out and err == ""
    target = tmp_path / "exports" / f"graph.{fmt}"
    code, out2, err = run("export", crawl_file, "--to", fmt, "-o", target)
    assert code == EXIT_OK and out2 == "" and f"Wrote {target}" in err
    assert target.read_text(encoding="utf-8") == out


def test_export_formats_are_valid(crawl_file):
    assert json.loads(run("export", crawl_file, "--to", "json")[1])["summary"]["pages"] == 7
    root = ET.fromstring(run("export", crawl_file, "--to", "graphml")[1])
    assert len(list(root.iter("{http://graphml.graphdrawing.org/xmlns}node"))) == 7
    assert run("export", crawl_file, "--to", "dot")[1].startswith("digraph")
    limited = run("export", crawl_file, "--to", "dot", "--max-nodes", "2")[1]
    assert limited.count("label=") == 2
    with_suggestions = run("export", crawl_file, "--to", "json", "--suggestions",
                           "--min-similarity", "0.01")[1]
    assert json.loads(with_suggestions)["suggestions"]


def test_export_csv_bundle(crawl_file, tmp_path):
    directory = tmp_path / "bundle"
    code, out, _err = run("export", crawl_file, "--to", "csv", "-o", directory, "--suggestions")
    assert code == EXIT_OK
    names = sorted(path.name for path in directory.iterdir())
    assert names == ["edges.csv", "issues.csv", "nodes.csv", "suggestions.csv"]
    assert out.count("Wrote ") == 4
    with (directory / "nodes.csv").open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 7


def test_export_csv_defaults_to_export_directory(crawl_file, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run("export", crawl_file, "--to", "csv")[0] == EXIT_OK
    assert (tmp_path / "export" / "nodes.csv").is_file()
    assert not (tmp_path / "export" / "suggestions.csv").exists()


def test_diff_command(tmp_path):
    old = build_site([("home", "a"), ("a", "home"), ("a", "b"), ("b", "home")], start="home")
    new = build_site([("home", "a"), ("a", "home"), ("home", "b"), ("b", "home"), ("b", "c"),
                      ("c", "home")], start="home")
    old_path, new_path, json_path = (tmp_path / n for n in ("old.json", "new.json", "d.json"))
    old.save(old_path)
    new.save(new_path)
    code, out, err = run("diff", old_path, new_path, "--json", json_path)
    assert code == EXIT_OK
    assert "Pages: 3 -> 4 (+1 / -0)" in out and "New pages (1)" in out
    assert f"{BASE}/b  2 -> 1" in out and f"Wrote {json_path}" in err
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["format"] == "internal-link-graph/diff" and data["new_pages"] == [f"{BASE}/c"]
    code, out, _err = run("diff", old_path, old_path)
    assert code == EXIT_OK and "No structural changes." in out


def test_diff_accepts_edge_lists_and_reports_missing_files(tmp_path):
    other = tmp_path / "edges.csv"
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    other.write_text("\n".join(lines[:-3]) + "\n", encoding="utf-8")
    code, out, _err = run("diff", EXAMPLE, other, "--top", "3")
    assert code == EXIT_OK and "Lost links (3)" in out
    code, _out, err = run("diff", EXAMPLE, tmp_path / "nope.csv")
    assert code == EXIT_ERROR and "input file not found" in err


def test_module_entry_point(crawl_file):
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-m", "internal_link_graph", "analyze", str(crawl_file), "--top", "2"],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0 and "Top 2 pages by PageRank" in completed.stdout
