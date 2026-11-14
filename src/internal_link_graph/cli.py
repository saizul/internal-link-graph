"""Command line interface: ``internal-link-graph <command>``."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from . import __version__
from .analyzer import AnalysisConfig, AnalysisResult, analyze
from .crawler import CrawlConfig, Crawler, CrawlError
from .diff import diff_sites, format_diff
from .exporters import (
    analysis_to_json,
    to_dot,
    to_graphml,
    write_csv_bundle,
    write_suggestions_csv,
)
from .fetcher import DEFAULT_USER_AGENT, Fetcher, UrllibFetcher
from .htmlreport import to_html
from .importers import FORMATS, ImportFormatError, load_site
from .models import SiteData
from .report import format_summary, to_markdown
from .robots import RobotsRules, RobotsVerdict, fetch_robots, rules_for_response
from .suggest import SuggestConfig, Suggestion, suggest_links
from .urlnorm import NormalizeOptions, origin_of

EXPORT_FORMATS = ("json", "csv", "graphml", "dot", "markdown", "html")
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_ISSUES = 3


class CliError(Exception):
    """A user-facing error; printed without a traceback."""


# -- argument parsing ----------------------------------------------------------


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _non_negative_float(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def _unit_float(text: str) -> float:
    value = float(text)
    if not 0 <= value <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return value


def _regex(text: str) -> str:
    try:
        re.compile(text)
    except re.error as exc:
        raise argparse.ArgumentTypeError(f"invalid regular expression: {exc}") from exc
    return text


def _add_input_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=FORMATS,
        default="auto",
        help="input format (default: detect from the file contents)",
    )
    parser.add_argument("--start-url", help="homepage URL (default: taken from the input)")


def _add_analysis_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("analysis options")
    group.add_argument("--damping", type=_unit_float, default=0.85, help="PageRank damping factor")
    group.add_argument(
        "--personalized",
        action="store_true",
        help="also compute PageRank personalised on the start URL",
    )
    group.add_argument(
        "--include-nofollow",
        action="store_true",
        help="let nofollow links count as graph edges",
    )
    group.add_argument(
        "--max-outlinks", type=_positive_int, default=100, help="outlinks per page before warning"
    )
    group.add_argument(
        "--deep-threshold", type=_positive_int, default=4, help="click depth considered too deep"
    )
    group.add_argument(
        "--cluster-depth",
        type=_positive_int,
        default=1,
        help="number of URL directory levels that define a topic cluster",
    )


def _add_suggest_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("suggestion options")
    group.add_argument(
        "--max-suggestions", type=_positive_int, default=50, help="maximum suggestions"
    )
    group.add_argument(
        "--min-similarity", type=_unit_float, default=0.1, help="minimum cosine similarity"
    )
    group.add_argument(
        "--min-depth", type=int, default=3, help="targets at this click depth or deeper qualify"
    )
    group.add_argument(
        "--max-inlinks", type=int, default=2, help="targets with at most this many inlinks qualify"
    )
    group.add_argument(
        "--unicode",
        action="store_true",
        help="Unicode-aware tokenisation (needed for Bangla and other non-Latin scripts)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="internal-link-graph",
        description="Crawl a site, build its internal link graph and report SEO problems.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")

    crawl = commands.add_parser("crawl", help="crawl a website and save the result as JSON")
    crawl.add_argument("url", help="start URL, for example https://example.com/")
    crawl.add_argument("-o", "--output", default="crawl.json", help="output file (crawl.json)")
    crawl.add_argument("--max-pages", type=_positive_int, default=500)
    crawl.add_argument("--max-depth", type=int, default=None)
    crawl.add_argument(
        "--delay", type=_non_negative_float, default=0.5, help="seconds between requests per host"
    )
    crawl.add_argument("--workers", type=_positive_int, default=4)
    crawl.add_argument("--timeout", type=_non_negative_float, default=15.0)
    crawl.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    crawl.add_argument("--ignore-robots", action="store_true", help="do not obey robots.txt")
    crawl.add_argument(
        "--include", action="append", default=[], type=_regex, metavar="REGEX",
        help="only crawl URLs matching this pattern (repeatable)",
    )
    crawl.add_argument(
        "--exclude", action="append", default=[], type=_regex, metavar="REGEX",
        help="never crawl URLs matching this pattern (repeatable)",
    )
    crawl.add_argument("--include-subdomains", action="store_true")
    crawl.add_argument("--no-sitemap", action="store_true", help="do not seed from sitemaps")
    crawl.add_argument(
        "--sitemap", action="append", default=[], metavar="URL", help="extra sitemap (repeatable)"
    )
    crawl.add_argument("--state", metavar="FILE", help="SQLite file that makes the crawl resumable")
    crawl.add_argument("--resume", action="store_true", help="continue the crawl stored in --state")
    crawl.add_argument(
        "--trailing-slash", choices=("keep", "add", "strip"), default="keep",
        help="how to treat trailing slashes when normalising URLs",
    )
    crawl.add_argument("--keep-tracking-params", action="store_true")
    crawl.add_argument("--strip-query", action="store_true", help="drop all query strings")
    crawl.add_argument("--no-text", action="store_true", help="do not store page text in the file")
    crawl.add_argument("-q", "--quiet", action="store_true", help="no progress output")

    analyze_cmd = commands.add_parser("analyze", help="analyse a crawl or link export")
    analyze_cmd.add_argument("input", help="crawl JSON, edge list (CSV/JSON) or Screaming Frog CSV")
    _add_input_options(analyze_cmd)
    _add_analysis_options(analyze_cmd)
    analyze_cmd.add_argument("--top", type=_positive_int, default=10, help="rows per table")
    analyze_cmd.add_argument("--json", metavar="FILE", help="also write the analysis as JSON")
    analyze_cmd.add_argument("--markdown", metavar="FILE", help="also write a Markdown report")
    analyze_cmd.add_argument("--html", metavar="FILE", help="also write an HTML report")
    analyze_cmd.add_argument(
        "--suggestions", action="store_true", help="include link suggestions in the reports"
    )
    analyze_cmd.add_argument(
        "--fail-on",
        choices=("error", "warning"),
        help=f"exit with status {EXIT_ISSUES} when issues of this severity (or worse) exist",
    )

    suggest = commands.add_parser("suggest", help="suggest internal links worth adding")
    suggest.add_argument("input")
    _add_input_options(suggest)
    _add_analysis_options(suggest)
    _add_suggest_options(suggest)
    suggest.add_argument("--output-format", choices=("text", "csv", "json"), default="text")
    suggest.add_argument("-o", "--output", metavar="FILE", help="write to a file instead of stdout")

    export = commands.add_parser("export", help="export the analysis in another format")
    export.add_argument("input")
    _add_input_options(export)
    _add_analysis_options(export)
    _add_suggest_options(export)
    export.add_argument("--to", choices=EXPORT_FORMATS, required=True, help="output format")
    export.add_argument(
        "-o", "--output", metavar="PATH",
        help="output file (a directory for csv); default: stdout, or ./export for csv",
    )
    export.add_argument(
        "--suggestions", action="store_true", help="include link suggestions where supported"
    )
    export.add_argument(
        "--max-nodes", type=_positive_int, default=200, help="node limit for the DOT export"
    )

    diff = commands.add_parser("diff", help="compare two crawls")
    diff.add_argument("old")
    diff.add_argument("new")
    _add_input_options(diff)
    _add_analysis_options(diff)
    diff.add_argument("--top", type=_positive_int, default=15)
    diff.add_argument("--json", metavar="FILE", help="also write the comparison as JSON")

    robots = commands.add_parser(
        "robots", help="check which URLs a robots.txt allows, and which rule decides"
    )
    robots.add_argument(
        "source", help="a local robots.txt file, or a site URL whose /robots.txt is fetched"
    )
    robots.add_argument("urls", nargs="*", metavar="URL", help="URLs or paths to check")
    robots.add_argument(
        "--urls-file", metavar="FILE", help="read more URLs from a file (one per line, # comments)"
    )
    robots.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    robots.add_argument("--timeout", type=_non_negative_float, default=15.0)
    robots.add_argument("--output-format", choices=("text", "json"), default="text")
    robots.add_argument(
        "--fail-on-blocked", action="store_true",
        help=f"exit with status {EXIT_ISSUES} when any checked URL is disallowed",
    )
    return parser


# -- helpers -------------------------------------------------------------------


def _analysis_config(args: argparse.Namespace) -> AnalysisConfig:
    return AnalysisConfig(
        damping=args.damping,
        personalized=args.personalized,
        follow_only=not args.include_nofollow,
        max_outlinks=args.max_outlinks,
        deep_threshold=args.deep_threshold,
        cluster_depth=args.cluster_depth,
        start_url=getattr(args, "start_url", None) or "",
    )


def _suggest_config(args: argparse.Namespace) -> SuggestConfig:
    return SuggestConfig(
        max_suggestions=args.max_suggestions,
        min_similarity=args.min_similarity,
        min_depth=args.min_depth,
        max_inlinks=args.max_inlinks,
        unicode_tokens=args.unicode,
    )


def _load(path: str, args: argparse.Namespace) -> SiteData:
    if not Path(path).is_file():
        raise CliError(f"input file not found: {path}")
    try:
        return load_site(path, fmt=args.format, start_url=args.start_url)
    except ImportFormatError as exc:
        raise CliError(f"cannot read {path}: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError) as exc:
        raise CliError(f"cannot read {path}: {type(exc).__name__}: {exc}") from exc


def _analyze(path: str, args: argparse.Namespace) -> AnalysisResult:
    site = _load(path, args)
    if not site.pages:
        raise CliError(f"{path} does not contain any pages or links")
    return analyze(site, _analysis_config(args))


def _write(path: str | None, text: str, stdout: TextIO) -> None:
    if path:
        target = Path(path)
        if target.parent != Path("."):
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    else:
        stdout.write(text)


def format_suggestions(suggestions: Sequence[Suggestion]) -> str:
    if not suggestions:
        return "No link suggestions: no weakly linked page has a similar, unlinked page.\n"
    lines = [f"{len(suggestions)} link suggestion{'s' if len(suggestions) != 1 else ''}", ""]
    for number, suggestion in enumerate(suggestions, 1):
        lines.append(f"{number}. Link from {suggestion.source}")
        lines.append(f"   to        {suggestion.target}")
        lines.append(f"   anchor    \"{suggestion.anchor}\"")
        lines.append(
            f"   why       similarity {suggestion.similarity:.2f}; {suggestion.reason}"
        )
        if suggestion.shared_terms:
            lines.append(f"   shared    {', '.join(suggestion.shared_terms)}")
        lines.append("")
    return "\n".join(lines)


# -- commands ------------------------------------------------------------------


def cmd_crawl(args: argparse.Namespace, stdout: TextIO, stderr: TextIO,
              fetcher: Fetcher | None) -> int:
    if args.resume and not args.state:
        raise CliError("--resume needs --state FILE")
    options = NormalizeOptions(
        trailing_slash=args.trailing_slash,
        strip_tracking=not args.keep_tracking_params,
        strip_query=args.strip_query,
    )
    try:
        config = CrawlConfig(
            start_url=args.url,
            max_pages=args.max_pages,
            max_depth=args.max_depth,
            delay=args.delay,
            workers=args.workers,
            user_agent=args.user_agent,
            respect_robots=not args.ignore_robots,
            include=tuple(args.include),
            exclude=tuple(args.exclude),
            include_subdomains=args.include_subdomains,
            use_sitemap=not args.no_sitemap,
            sitemap_urls=tuple(args.sitemap),
            normalize=options,
            state_path=args.state,
            resume=args.resume,
            timeout=args.timeout,
        )
    except ValueError as exc:
        raise CliError(str(exc)) from exc

    counter = {"page": 0}

    def progress(kind: str, url: str, detail: str = "") -> None:
        if args.quiet:
            return
        if kind == "page":
            counter["page"] += 1
            stderr.write(f"[{counter['page']:>5}] {detail or '200'} {url}\n")
        elif kind in ("error", "blocked", "sitemap-error") or kind in ("sitemap", "resume"):
            stderr.write(f"[{kind}] {url} {detail}\n".rstrip() + "\n")

    if fetcher is None:
        fetcher = UrllibFetcher(user_agent=args.user_agent, timeout=args.timeout)
    try:
        crawler = Crawler(config, fetcher=fetcher, progress=progress)
        site = crawler.crawl()
    except CrawlError as exc:
        raise CliError(str(exc)) from exc
    except KeyboardInterrupt:
        stderr.write("Interrupted.\n")
        if args.state:
            stderr.write(f"Run again with --state {args.state} --resume to continue.\n")
        return 130
    output = Path(args.output)
    if output.parent != Path("."):
        output.parent.mkdir(parents=True, exist_ok=True)
    site.save(output, include_text=not args.no_text)
    stats = crawler.stats
    stdout.write(
        f"Crawled {stats.pages_crawled} pages ({stats.redirects} redirects, "
        f"{stats.errors} errors, {stats.blocked_by_robots} blocked by robots.txt) "
        f"in {stats.elapsed:.1f}s\n"
    )
    stdout.write(f"Found {sum(1 for link in site.links if link.internal)} internal links\n")
    if stats.frontier_remaining:
        stdout.write(
            f"{stats.frontier_remaining} discovered URLs were not crawled (limit reached)\n"
        )
    stdout.write(f"Saved {output}\n")
    return EXIT_OK


def cmd_analyze(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    result = _analyze(args.input, args)
    suggestions = suggest_links(result.site, result) if args.suggestions else None
    stdout.write(format_summary(result, suggestions, top=args.top))
    if args.json:
        _write(args.json, analysis_to_json(result, suggestions) + "\n", stdout)
    if args.markdown:
        _write(args.markdown, to_markdown(result, suggestions), stdout)
    if args.html:
        _write(args.html, to_html(result, suggestions), stdout)
    for path in (args.json, args.markdown, args.html):
        if path:
            stderr.write(f"Wrote {path}\n")
    if args.fail_on:
        severities = result.summary["issues_by_severity"]
        failing = severities.get("error", 0)
        if args.fail_on == "warning":
            failing += severities.get("warning", 0)
        if failing:
            return EXIT_ISSUES
    return EXIT_OK


def cmd_suggest(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    result = _analyze(args.input, args)
    suggestions = suggest_links(result.site, result, _suggest_config(args))
    if args.output_format == "json":
        payload = [suggestion.to_dict() for suggestion in suggestions]
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        _write(args.output, text, stdout)
    elif args.output_format == "csv":
        if args.output:
            with Path(args.output).open("w", encoding="utf-8", newline="") as handle:
                write_suggestions_csv(suggestions, handle)
        else:
            write_suggestions_csv(suggestions, stdout)
    else:
        _write(args.output, format_suggestions(suggestions), stdout)
    if args.output:
        stderr.write(f"Wrote {len(suggestions)} suggestions to {args.output}\n")
    return EXIT_OK


def cmd_export(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    result = _analyze(args.input, args)
    suggestions = (
        suggest_links(result.site, result, _suggest_config(args)) if args.suggestions else None
    )
    if args.to == "csv":
        directory = args.output or "export"
        written = write_csv_bundle(result, directory, suggestions)
        for path in written.values():
            stdout.write(f"Wrote {path}\n")
        return EXIT_OK
    if args.to == "json":
        text = analysis_to_json(result, suggestions) + "\n"
    elif args.to == "graphml":
        text = to_graphml(result)
    elif args.to == "dot":
        text = to_dot(result, max_nodes=args.max_nodes)
    elif args.to == "markdown":
        text = to_markdown(result, suggestions)
    else:
        text = to_html(result, suggestions)
    _write(args.output, text, stdout)
    if args.output:
        stderr.write(f"Wrote {args.output}\n")
    return EXIT_OK


def cmd_diff(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    old = _load(args.old, args)
    new = _load(args.new, args)
    diff = diff_sites(old, new, _analysis_config(args), top=args.top)
    stdout.write(format_diff(diff, limit=args.top))
    if args.json:
        _write(args.json, json.dumps(diff.to_dict(), ensure_ascii=False, indent=2) + "\n", stdout)
        stderr.write(f"Wrote {args.json}\n")
    return EXIT_OK


def _robots_source(
    args: argparse.Namespace, fetcher: Fetcher | None
) -> tuple[str, RobotsRules]:
    source = args.source
    if re.match(r"^https?://", source, re.IGNORECASE):
        if fetcher is None:
            fetcher = UrllibFetcher(user_agent=args.user_agent, timeout=args.timeout)
        return origin_of(source) + "/robots.txt", fetch_robots(fetcher, source)
    try:
        text = Path(source).read_bytes()
    except OSError as exc:
        raise CliError(f"cannot read {source}: {exc.strerror or exc}") from exc
    rules = rules_for_response(200, text)
    rules.status = 0  # a local file has no HTTP status
    return source, rules


def _robots_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.urls)
    if args.urls_file:
        try:
            lines = Path(args.urls_file).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise CliError(f"cannot read {args.urls_file}: {exc}") from exc
        urls.extend(line.strip() for line in lines if line.strip() and not line.startswith("#"))
    if not urls:
        raise CliError("give at least one URL to check (or --urls-file FILE)")
    return urls


def format_robots_report(
    label: str, rules: RobotsRules, user_agent: str, verdicts: Sequence[RobotsVerdict]
) -> str:
    lines = [f"robots.txt: {label}" + (f" (status {rules.status})" if rules.status else "")]
    if rules.truncated:
        lines.append("warning: file is larger than 500 KiB; the rest was ignored")
    delay = rules.crawl_delay(user_agent)
    lines.append(f"User agent: {user_agent}")
    if delay is not None:
        lines.append(f"Crawl-delay: {delay:g}")
    if rules.sitemaps:
        lines.append(f"Sitemaps: {len(rules.sitemaps)}")
    lines.append("")
    width = max(len(verdict.url) for verdict in verdicts)
    for verdict in verdicts:
        status = "allowed" if verdict.allowed else "blocked"
        why = str(verdict.rule) if verdict.rule is not None else verdict.reason
        if verdict.agents:
            why += f"  [group: {', '.join(verdict.agents)}]"
        lines.append(f"{status}  {verdict.url:<{width}}  {why}".rstrip())
    blocked = sum(1 for verdict in verdicts if not verdict.allowed)
    lines.append("")
    lines.append(f"{len(verdicts) - blocked} allowed, {blocked} blocked")
    return "\n".join(lines) + "\n"


def cmd_robots(args: argparse.Namespace, stdout: TextIO, stderr: TextIO,
               fetcher: Fetcher | None) -> int:
    urls = _robots_urls(args)
    label, rules = _robots_source(args, fetcher)
    verdicts = [rules.check(args.user_agent, url) for url in urls]
    if args.output_format == "json":
        payload = {
            "robots_txt": label,
            "status": rules.status or None,
            "user_agent": args.user_agent,
            "crawl_delay": rules.crawl_delay(args.user_agent),
            "sitemaps": list(rules.sitemaps),
            "truncated": rules.truncated,
            "results": [verdict.to_dict() for verdict in verdicts],
        }
        stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        stdout.write(format_robots_report(label, rules, args.user_agent, verdicts))
    if args.fail_on_blocked and any(not verdict.allowed for verdict in verdicts):
        return EXIT_ISSUES
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    fetcher: Fetcher | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Entry point. ``fetcher`` lets tests run ``crawl`` and ``robots`` offline."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if not args.command:
        parser.print_help(stdout)
        return EXIT_USAGE
    try:
        if args.command == "crawl":
            return cmd_crawl(args, stdout, stderr, fetcher)
        if args.command == "analyze":
            return cmd_analyze(args, stdout, stderr)
        if args.command == "suggest":
            return cmd_suggest(args, stdout, stderr)
        if args.command == "export":
            return cmd_export(args, stdout, stderr)
        if args.command == "diff":
            return cmd_diff(args, stdout, stderr)
        if args.command == "robots":
            return cmd_robots(args, stdout, stderr, fetcher)
    except CliError as exc:
        stderr.write(f"error: {exc}\n")
        return EXIT_ERROR
    except BrokenPipeError:
        return EXIT_OK
    parser.error(f"unknown command {args.command!r}")
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
