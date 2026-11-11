# Contributing

Bug reports, fixes and improvements are welcome.

## Setting up

```console
$ git clone https://github.com/saizul/internal-link-graph.git
$ cd internal-link-graph
$ python -m venv .venv && source .venv/bin/activate
$ pip install -e ".[dev]"
```

## Before opening a pull request

```console
$ ruff check .
$ pytest
```

Both must pass. CI runs them on Python 3.10, 3.11, 3.12 and 3.13.

## Ground rules

- **No runtime dependencies.** The package uses the standard library only. Development
  tools (pytest, ruff) belong in the `dev` extra.
- **Tests never use the network.** Crawler tests serve an in-memory site through
  `DictFetcher` (see `tests/conftest.py`). Use `example.com` style URLs in tests, examples
  and documentation.
- **Every behaviour change needs a test.** For graph algorithms, prefer small graphs whose
  expected values can be worked out by hand and say how in the test.
- **Keep the output deterministic.** Sort where iteration order would otherwise leak into
  reports, so that two runs over the same input produce identical files.
- Python 3.10 is the oldest supported version; do not use newer syntax or library features.
- Lines are at most 100 characters; ruff enforces import order and the rest of the style.

## Project layout

| Path | Contents |
| --- | --- |
| `src/internal_link_graph/crawler.py` | Crawl loop, frontier, redirect handling |
| `src/internal_link_graph/fetcher.py` | HTTP fetcher and the in-memory `DictFetcher` |
| `src/internal_link_graph/html_extract.py` | Link and metadata extraction from HTML |
| `src/internal_link_graph/urlnorm.py` | URL normalisation |
| `src/internal_link_graph/robots.py`, `sitemap.py`, `ratelimit.py`, `storage.py` | Crawl support |
| `src/internal_link_graph/importers.py` | Screaming Frog and edge list importers |
| `src/internal_link_graph/graph.py` | Graph structure, PageRank, BFS, Tarjan SCC, HITS |
| `src/internal_link_graph/equity.py`, `anchors.py`, `clusters.py` | Individual analyses |
| `src/internal_link_graph/analyzer.py` | Combines everything into an `AnalysisResult` |
| `src/internal_link_graph/suggest.py`, `textsim.py` | TF-IDF link suggestions |
| `src/internal_link_graph/exporters.py`, `report.py`, `htmlreport.py` | Output formats |
| `src/internal_link_graph/diff.py` | Crawl comparison |
| `src/internal_link_graph/cli.py` | Command line interface |

## Adding an issue type

1. Add the code, severity and title to `ISSUE_TYPES` in `analyzer.py` (the order there is the
   order used in reports).
2. Emit `Issue(code, url, detail, target)` from the analysis.
3. Add tests for the positive and the negative case, and mention the check in the README.

## Reporting bugs

Please include the command you ran, the version (`internal-link-graph --version`), what you
expected and what happened. A minimal edge list or HTML snippet that reproduces the problem
is the fastest way to a fix. Do not post crawl data of sites you do not own.

## License

By contributing you agree that your contributions are licensed under the MIT License.
