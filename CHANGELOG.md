# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
