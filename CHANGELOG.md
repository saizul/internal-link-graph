# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `robots` command: checks URLs or paths against a local robots.txt file or a site's live
  `/robots.txt` and reports, per URL, whether it is allowed and which line decided it.
  Supports `--user-agent`, `--urls-file`, `--output-format text|json` and
  `--fail-on-blocked` (exit status 3) for CI checks.
- `parse_robots`, `RobotsRules` and `RobotsVerdict` are exported from the package;
  `RobotsRules.check()` explains a decision.

### Changed

- robots.txt is now parsed by an RFC 9309 implementation instead of
  `urllib.robotparser`: `*` and `$` wildcards are honoured, the longest matching rule wins
  (with `Allow` winning ties) instead of the first one, groups for the same user agent are
  merged, user agents are matched on their full product token instead of a substring,
  percent-encoding is normalised, `/robots.txt` is always allowed and only the first 500 KiB
  of the file are parsed. Crawls of sites that rely on these rules can fetch a different set
  of URLs than before.

## [0.1.0] - 2026-11-11

### Added

- Crawler with robots.txt support (including `Crawl-delay`), per-host rate limiting,
  sitemap seeding, redirect chain tracking, include/exclude patterns, page and depth
  limits, concurrent workers and resumable state in SQLite.
- HTML extraction of links, anchor text, `rel` values, link position, canonical tags,
  meta robots, titles, H1s and body text.
- URL normalisation with configurable trailing slash, tracking parameter and query handling.
- Importers for Screaming Frog "All Inlinks" CSV exports and CSV/JSON edge lists.
- Graph analysis: PageRank, personalised PageRank, click depth, orphan and unreachable
  pages, dead ends, strongly connected components, HITS hubs and authorities.
- Link equity loss through broken links, redirects and nofollow links.
- Anchor text distribution with generic, empty and over-optimised anchor detection.
- Checks for excessive outlinks, links to noindex and canonicalised pages, duplicate and
  near-duplicate titles, missing titles and H1s.
- Topic clusters by URL directory with cohesion scores.
- TF-IDF based internal link suggestions with optional Unicode tokenisation.
- Output as terminal summary, JSON, CSV, GraphML, DOT, Markdown and a self-contained HTML
  report with inline SVG charts.
- `crawl`, `analyze`, `suggest`, `export` and `diff` commands.

[Unreleased]: https://github.com/saizul/internal-link-graph/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saizul/internal-link-graph/releases/tag/v0.1.0
