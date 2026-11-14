"""RFC 9309 semantics of the robots.txt parser and the ``robots`` command."""

import io
import json
import time

import pytest

from internal_link_graph.cli import EXIT_ERROR, EXIT_ISSUES, EXIT_OK, main
from internal_link_graph.fetcher import DEFAULT_USER_AGENT, DictFetcher
from internal_link_graph.robots import (
    MAX_ROBOTS_BYTES,
    RobotsRule,
    parse_robots,
    product_token,
    rules_for_response,
    url_path,
)

BASE = "https://example.com"


def allowed(robots: str, path: str, agent: str = "testbot/1.0") -> bool:
    return parse_robots(robots).can_fetch(agent, BASE + path)


# -- pattern matching ------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("/fish", "/fish", True),
        ("/fish", "/fish.html", True),
        ("/fish", "/fish/salmon.html", True),
        ("/fish", "/Fish.asp", False),  # paths are case-sensitive
        ("/fish", "/catfish", False),
        ("/fish*", "/fishheads", True),
        ("/*.php", "/index.php", True),
        ("/*.php", "/folder/filename.php?parameters", True),
        ("/*.php", "/windows.PHP", False),
        ("/*.php$", "/filename.php", True),
        ("/*.php$", "/filename.php?parameters", False),
        ("/*.php$", "/filename.php5", False),
        ("/fish*.php", "/fish.php", True),
        ("/fish*.php", "/fishheads/catfish.php?parameters", True),
        ("/fish*.php", "/Fish.PHP", False),
        ("/$", "/", True),
        ("/$", "/page", False),
        ("*", "/anything", True),
        ("/*/private/*", "/a/private/b", True),
        ("/*/private/*", "/private/b", False),
        ("/a*b*c$", "/a-b-c", True),
        ("/a*b*c$", "/a-b-c-d", False),
        ("/a*b$", "/ab", True),
        ("/ab*b$", "/ab", False),  # the anchored tail may not overlap the prefix
        ("/**x", "/yx", True),
        ("/?q=", "/?q=1", True),
    ],
)
def test_rule_pattern_matching(pattern, path, expected):
    assert RobotsRule(False, pattern).matches(url_path(BASE + path)) is expected


def test_pattern_matching_is_linear_on_hostile_input():
    rule = RobotsRule(False, "/" + "*a" * 200 + "$")
    path = "/" + "a" * 50_000 + "b"
    start = time.perf_counter()
    assert not rule.matches(path)
    assert time.perf_counter() - start < 1.0


def test_wildcards_in_full_robots_file():
    robots = "User-agent: *\nDisallow: /*?sessionid=\nDisallow: /*&sessionid=\nDisallow: /*.pdf$\n"
    assert not allowed(robots, "/shop?sessionid=42")
    assert not allowed(robots, "/shop/item?color=red&sessionid=42")
    assert allowed(robots, "/shop/sessionid=42")
    assert allowed(robots, "/shop?color=red")
    assert not allowed(robots, "/files/report.pdf")
    assert allowed(robots, "/files/report.pdf?download=1")


# -- precedence ------------------------------------------------------------------


def test_longest_match_wins_regardless_of_order():
    robots = "User-agent: *\nDisallow: /\nAllow: /blog/\n"
    assert allowed(robots, "/blog/post")
    assert not allowed(robots, "/shop/")
    # The standard library would apply the first matching line instead.
    robots = "User-agent: *\nAllow: /page\nDisallow: /*.htm\n"
    assert not allowed(robots, "/page.htm")


def test_allow_wins_a_tie():
    robots = "User-agent: *\nDisallow: /folder\nAllow: /folder\n"
    assert allowed(robots, "/folder/page")
    robots = "User-agent: *\nAllow: /$\nDisallow: /\n"
    assert allowed(robots, "/")
    assert not allowed(robots, "/page")


def test_verdict_names_the_deciding_rule():
    rules = parse_robots("User-agent: *\nDisallow: /private/\nAllow: /private/press/\n")
    verdict = rules.check("bot", BASE + "/private/press/kit.zip")
    assert verdict.allowed
    assert verdict.rule == RobotsRule(True, "/private/press/", 3)
    assert str(verdict.rule) == "line 3: Allow: /private/press/"
    assert verdict.agents == ("*",)
    blocked = rules.check("bot", BASE + "/private/x")
    assert not blocked.allowed and blocked.rule.line == 2
    assert rules.check("bot", BASE + "/public").reason == "no matching rule"


# -- groups and user agents ------------------------------------------------------


def test_product_token():
    assert product_token(DEFAULT_USER_AGENT) == "internal-link-graph"
    assert product_token("  Googlebot/2.1 (+http://www.google.com/bot.html)") == "googlebot"
    assert product_token("") == ""


def test_specific_group_replaces_the_star_group():
    robots = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: TestBot\nDisallow: /admin/\n"
    )
    assert allowed(robots, "/page", agent="testbot/2.0")
    assert not allowed(robots, "/admin/", agent="TESTBOT")
    assert not allowed(robots, "/page", agent="otherbot")


def test_user_agent_is_not_a_substring_match():
    robots = "User-agent: bot\nDisallow: /\n"
    assert allowed(robots, "/page", agent="testbot")
    assert not allowed(robots, "/page", agent="bot/1.0")


def test_groups_for_the_same_agent_are_merged():
    robots = (
        "User-agent: testbot\nDisallow: /a/\n\n"
        "User-agent: other\nDisallow: /b/\n\n"
        "User-agent: testbot\nDisallow: /c/\n"
    )
    assert not allowed(robots, "/a/x")
    assert allowed(robots, "/b/x")
    assert not allowed(robots, "/c/x")


def test_consecutive_user_agent_lines_share_a_group():
    robots = "User-agent: alpha\nUser-agent: testbot\nDisallow: /shared/\n"
    assert not allowed(robots, "/shared/1")
    assert not allowed(robots, "/shared/1", agent="alpha")
    assert allowed(robots, "/shared/1", agent="gamma")


def test_sitemap_lines_do_not_end_a_group():
    robots = (
        "User-agent: testbot\nSitemap: https://example.com/s.xml\nDisallow: /x/\n"
        "Sitemap: https://example.com/s.xml\n"
    )
    rules = parse_robots(robots)
    assert not rules.can_fetch("testbot", BASE + "/x/1")
    assert rules.sitemaps == ["https://example.com/s.xml"]


def test_rules_before_any_user_agent_are_ignored():
    assert allowed("Disallow: /\nUser-agent: other\nDisallow: /\n", "/page")


def test_no_group_at_all_allows_everything():
    rules = parse_robots("# nothing here\nSitemap: https://example.com/s.xml\n")
    verdict = rules.check("bot", BASE + "/x")
    assert verdict.allowed and verdict.reason == "no group for this user agent"


# -- syntax details --------------------------------------------------------------


def test_comments_whitespace_case_and_empty_values():
    robots = (
        "﻿USER-AGENT : *   # everyone\n"
        "disallow:   /tmp/   # temporary\n"
        "Disallow:\n"
        "Allow:\n"
        "garbage line without colon\n"
        "Unknown-directive: whatever\n"
        "Disallow: nolead\n"
    )
    assert not allowed(robots, "/tmp/file")
    assert allowed(robots, "/other")
    assert not allowed(robots, "/nolead")  # missing leading slash is added


def test_crawl_delay_per_group():
    robots = (
        "User-agent: *\nCrawl-delay: 5\nDisallow: /x/\n\n"
        "User-agent: testbot\nCrawl-delay: 1.5\nCrawl-delay: 9\n\n"
        "User-agent: badbot\nCrawl-delay: soon\n\n"
        "User-agent: negbot\nCrawl-delay: -3\n"
    )
    rules = parse_robots(robots)
    assert rules.crawl_delay("anybot") == 5
    assert rules.crawl_delay("testbot") == 1.5
    assert rules.crawl_delay("badbot") is None
    assert rules.crawl_delay("negbot") is None
    assert rules_for_response(404, b"").crawl_delay("bot") is None


def test_percent_encoding_is_normalised():
    robots = "User-agent: *\nDisallow: /café/\nDisallow: /%7euser/\nDisallow: /a%2fb\n"
    assert not allowed(robots, "/caf%C3%A9/menu")
    assert not allowed(robots, "/caf%c3%a9/menu")
    assert not allowed(robots, "/~user/home")
    assert not allowed(robots, "/a%2Fb")
    assert allowed(robots, "/a/b")  # an encoded slash is a different path


def test_robots_txt_itself_is_always_allowed():
    assert allowed("User-agent: *\nDisallow: /\n", "/robots.txt")


def test_large_files_are_truncated():
    padding = "# " + "x" * 1000 + "\n"
    body = ("User-agent: *\nDisallow: /early/\n" + padding * 600 + "Disallow: /late/\n").encode()
    assert len(body) > MAX_ROBOTS_BYTES
    rules = rules_for_response(200, body, "text/plain")
    assert rules.truncated
    assert not rules.can_fetch("bot", BASE + "/early/")
    assert rules.can_fetch("bot", BASE + "/late/")
    assert not parse_robots("User-agent: *\nDisallow: /\n").truncated


def test_status_policy_verdict_reasons():
    assert rules_for_response(503, b"").check("bot", BASE + "/").reason == "robots.txt status 503"
    assert rules_for_response(404, b"").check("bot", BASE + "/").allowed


# -- the robots command ----------------------------------------------------------

ROBOTS = (
    "User-agent: *\n"
    "Crawl-delay: 2\n"
    "Disallow: /private/\n"
    "Allow: /private/press/\n"
    "Disallow: /*.pdf$\n"
    "Sitemap: https://example.com/sitemap.xml\n"
)


def run(*argv, fetcher=None):
    out, err = io.StringIO(), io.StringIO()
    code = main([str(arg) for arg in argv], fetcher=fetcher, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def robots_file(tmp_path):
    path = tmp_path / "robots.txt"
    path.write_text(ROBOTS, encoding="utf-8")
    return path


def test_robots_command_text_report(robots_file):
    code, out, _err = run("robots", robots_file, "/", "/private/a", "/private/press/kit",
                          "/docs/a.pdf")
    assert code == EXIT_OK
    assert out.startswith(f"robots.txt: {robots_file}\n")
    assert "Crawl-delay: 2" in out
    assert "Sitemaps: 1" in out
    lines = out.splitlines()
    assert any(line.startswith("allowed  /  ") and "no matching rule" in line for line in lines)
    assert any(line.startswith("blocked  /private/a") and "line 3: Disallow" in line
               for line in lines)
    assert any(line.startswith("allowed  /private/press/kit") for line in lines)
    assert any(line.startswith("blocked  /docs/a.pdf") and "/*.pdf$" in line for line in lines)
    assert lines[-1] == "2 allowed, 2 blocked"


def test_robots_command_json_and_fail_on_blocked(robots_file, tmp_path):
    urls = tmp_path / "urls.txt"
    urls.write_text("# important pages\nhttps://example.com/\n\nhttps://example.com/private/x\n",
                    encoding="utf-8")
    code, out, _err = run("robots", robots_file, "--urls-file", urls, "--output-format", "json",
                          "--fail-on-blocked", "--user-agent", "testbot/1.0")
    assert code == EXIT_ISSUES
    payload = json.loads(out)
    assert payload["status"] is None
    assert payload["crawl_delay"] == 2
    assert payload["sitemaps"] == ["https://example.com/sitemap.xml"]
    assert [r["allowed"] for r in payload["results"]] == [True, False]
    assert payload["results"][1]["rule"] == {"directive": "Disallow", "pattern": "/private/",
                                             "line": 3}
    assert payload["results"][1]["group"] == ["*"]
    code, _out, _err = run("robots", robots_file, "/", "--fail-on-blocked")
    assert code == EXIT_OK


def test_robots_command_fetches_from_a_site():
    fetcher = DictFetcher(
        {f"{BASE}/robots.txt": (200, {"content-type": "text/plain"}, ROBOTS)}
    )
    code, out, _err = run("robots", f"{BASE}/some/page", f"{BASE}/private/x", fetcher=fetcher)
    assert code == EXIT_OK
    assert out.startswith("robots.txt: https://example.com/robots.txt (status 200)")
    assert "blocked  https://example.com/private/x" in out
    assert fetcher.requests == [f"{BASE}/robots.txt"]


def test_robots_command_errors(tmp_path, robots_file):
    code, _out, err = run("robots", tmp_path / "missing.txt", "/")
    assert code == EXIT_ERROR and "cannot read" in err
    code, _out, err = run("robots", robots_file)
    assert code == EXIT_ERROR and "at least one URL" in err


def test_crawler_obeys_wildcard_rules():
    from internal_link_graph import crawl

    pages = {
        f"{BASE}/robots.txt": (
            200,
            {"content-type": "text/plain"},
            "User-agent: *\nDisallow: /*?print=\nDisallow: /*-draft$\n",
        ),
        f"{BASE}/": (
            200,
            {"content-type": "text/html"},
            '<a href="/a">A</a> <a href="/a?print=1">Print</a> <a href="/post-draft">Draft</a>',
        ),
        f"{BASE}/a": (200, {"content-type": "text/html"}, "<h1>A</h1>"),
    }
    fetcher = DictFetcher(pages)
    site = crawl(f"{BASE}/", fetcher=fetcher, delay=0, use_sitemap=False)
    assert f"{BASE}/a" in fetcher.requests
    assert f"{BASE}/a?print=1" not in fetcher.requests
    assert f"{BASE}/post-draft" not in fetcher.requests
    blocked = {url for url, page in site.pages.items() if page.blocked_by_robots}
    assert blocked == {f"{BASE}/a?print=1", f"{BASE}/post-draft"}
