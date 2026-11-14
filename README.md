# internal-link-graph

[![CI](https://github.com/saizul/internal-link-graph/actions/workflows/ci.yml/badge.svg)](https://github.com/saizul/internal-link-graph/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

Crawl a website (or load a crawl export), build its **internal link graph** and find out
where link equity flows, where it leaks and which internal links are worth adding.

It is a command line tool and a Python library with **no runtime dependencies** - only the
standard library. PageRank, HITS, Tarjan's strongly connected components and TF-IDF are
implemented in the package itself.

```console
$ internal-link-graph crawl https://example.com/ -o crawl.json
$ internal-link-graph analyze crawl.json --html report.html
$ internal-link-graph suggest crawl.json
```

## Features

**Crawler**

- Polite by default: obeys `robots.txt` (including `Crawl-delay`), rate limits per host and
  identifies itself with a clear user agent.
- `robots.txt` is interpreted as RFC 9309 says: `*` and `$` wildcards, longest-match
  precedence with `Allow` winning ties, merged groups per user agent, normalised
  percent-encoding. `robots` checks URLs against a file and names the rule that decides.
- Seeds from `sitemap.xml` (sitemap indexes and gzip supported), so pages that exist but are
  never linked show up as orphans.
- Follows and records every hop of a redirect chain.
- Reads canonical tags, meta robots, `X-Robots-Tag`, `rel` values, anchor text, image `alt`
  text and the position of each link (navigation, header, footer, main content).
- URL normalisation: fragments, default ports, tracking parameters, parameter order,
  trailing slash policy.
- Include / exclude patterns, page and depth limits, concurrent workers.
- Resumable: `--state crawl.sqlite` stores progress, `--resume` continues after an interruption.

**Importers** - no crawl needed if you already have the data

- Screaming Frog "All Inlinks" CSV export.
- A plain edge list as CSV (`source,target[,anchor,...]`) or JSON.

**Analysis**

- PageRank (own implementation, dangling nodes handled) and optional PageRank personalised
  on the homepage.
- Click depth from the homepage (BFS), orphan pages, unreachable pages, dead ends.
- Strongly connected components (iterative Tarjan), hubs and authorities (HITS).
- Link equity lost to broken links, redirects and nofollow links.
- Anchor text distribution per target, generic and empty anchors, over-optimised anchors.
- Pages with excessive outlinks, links to noindex or canonicalised URLs, duplicate and
  near-duplicate titles, missing titles and H1s.
- Topic clusters by URL directory with a cohesion score.

**Link suggestions**

- TF-IDF cosine similarity between pages finds topically related pages that do not link to
  each other yet, prioritising targets that are deep or weakly linked and sources that have
  PageRank to give.
- `--unicode` switches to Unicode-aware tokenisation for Bangla and other non-Latin scripts.

**Output**

- Terminal summary, JSON, CSV (nodes, edges, issues, suggestions), GraphML (Gephi, yEd,
  Cytoscape), Graphviz DOT, Markdown and a self-contained HTML report with inline SVG charts
  (no JavaScript, no external assets).
- `diff` compares two crawls: new and lost pages and links, PageRank movers, depth changes.

## Installation

Python 3.10 or newer is required.

```console
$ git clone https://github.com/saizul/internal-link-graph.git
$ cd internal-link-graph
$ pip install .
```

For development (adds pytest and ruff):

```console
$ pip install -e ".[dev]"
```

## Quick start

Try it without crawling anything, using the bundled sample data:

```console
$ internal-link-graph analyze examples/sample-edges.csv
$ internal-link-graph suggest examples/sample-edges.json
$ internal-link-graph export examples/sample-edges.csv --to html -o report.html
```

Crawl your own site:

```console
$ internal-link-graph crawl https://example.com/ --max-pages 2000 --delay 0.5 -o crawl.json
$ internal-link-graph analyze crawl.json --suggestions --html report.html --markdown report.md
```

Only crawl sites you own or have permission to crawl, and keep the delay reasonable.

### Commands

| Command | Purpose |
| --- | --- |
| `crawl URL` | Crawl a site and save pages and links as JSON |
| `analyze INPUT` | Print the summary; optionally write JSON, Markdown and HTML reports |
| `suggest INPUT` | Propose internal links worth adding (text, CSV or JSON) |
| `export INPUT --to FORMAT` | Export as `json`, `csv`, `graphml`, `dot`, `markdown` or `html` |
| `diff OLD NEW` | Compare two crawls |
| `robots SOURCE URL...` | Check URLs against a robots.txt file or a site's live robots.txt |

`INPUT` is a crawl JSON file, an edge list (CSV or JSON) or a Screaming Frog CSV. The format is
detected automatically; force it with `--format crawl|edges|screaming-frog`.

Useful options (see `internal-link-graph COMMAND --help` for all of them):

```text
crawl     --max-pages N  --max-depth N  --delay SECONDS  --workers N
          --include REGEX  --exclude REGEX  --include-subdomains
          --no-sitemap  --sitemap URL  --ignore-robots
          --state FILE  --resume
          --trailing-slash keep|add|strip  --strip-query  --keep-tracking-params
analyze   --damping 0.85  --personalized  --include-nofollow
          --max-outlinks 100  --deep-threshold 4  --cluster-depth 1
          --json FILE  --markdown FILE  --html FILE  --suggestions
          --fail-on error|warning
suggest   --max-suggestions 50  --min-similarity 0.1  --min-depth 3  --max-inlinks 2
          --unicode  --output-format text|csv|json  -o FILE
export    --to FORMAT  -o PATH  --suggestions  --max-nodes 200
diff      --top 15  --json FILE
robots    --user-agent UA  --urls-file FILE  --output-format text|json
          --fail-on-blocked
```

`analyze --fail-on error` exits with status 3 when errors are found, which makes the tool
usable as a CI check against a staging site. `robots --fail-on-blocked` does the same when a
checked URL is disallowed. Other exit codes: 0 success, 1 runtime error
(unreadable input, invalid crawl settings), 2 usage error.

### Resuming a crawl

```console
$ internal-link-graph crawl https://example.com/ --state crawl.sqlite -o crawl.json
^C
$ internal-link-graph crawl https://example.com/ --state crawl.sqlite --resume -o crawl.json
```

### Testing robots.txt rules

`robots` answers "would the crawler fetch this URL, and why?" for a local file (handy before
deploying a new robots.txt) or for a live site, whose `/robots.txt` is fetched:

```console
$ internal-link-graph robots robots.txt / /private/report /private/press/kit /docs/a.pdf
robots.txt: robots.txt
User agent: internal-link-graph/0.1.0 (+https://github.com/saizul/internal-link-graph)
Crawl-delay: 2
Sitemaps: 1

allowed  /                   no matching rule  [group: *]
blocked  /private/report     line 3: Disallow: /private/  [group: *]
allowed  /private/press/kit  line 4: Allow: /private/press/  [group: *]
blocked  /docs/a.pdf         line 5: Disallow: /*.pdf$  [group: *]

2 allowed, 2 blocked
$ internal-link-graph robots https://example.com/ --urls-file important.txt --fail-on-blocked
```

`--user-agent` checks the rules as another crawler would see them (only the product token,
such as `Googlebot`, is used to pick the group). `--output-format json` gives the same result
in machine-readable form. The crawler applies exactly these rules:

- A group is chosen by the crawler's product token, compared case-insensitively and in full
  (`bot` does not match `testbot`). All groups naming that token are merged; the `*` group
  is only used when none does, and no group at all means everything is allowed.
- `*` matches any run of characters and a trailing `$` anchors the pattern at the end of the
  path and query. Patterns are case-sensitive.
- The longest matching pattern decides. If an `Allow` and a `Disallow` pattern are equally
  long, `Allow` wins. Rule order in the file does not matter.
- Percent-encoding is normalised on both sides (`/caf%C3%A9` equals `/café`, `%7e` equals
  `~`), but encoded reserved characters such as `%2F` stay distinct.
- `/robots.txt` is always allowed, rules before the first `User-agent` line are ignored,
  and only the first 500 KiB of the file are read.
- A 401 or 403 response blocks the whole site, any other 4xx allows everything, and a 5xx or
  network error blocks the site for the rest of the crawl.

### Edge list formats

CSV needs a header with a source and a target column (`source`/`from`, `target`/`to`/
`destination`); `anchor`, `rel`, `nofollow` and `position` columns are optional:

```csv
source,target,anchor
https://example.com/,https://example.com/blog/,Blog
https://example.com/blog/,https://example.com/,Home
```

JSON can be a list of `[source, target]` pairs, a list of objects, or an object that also
carries page details - see [`examples/sample-edges.json`](examples/sample-edges.json). Titles
and text are what the link suggestions are computed from, so an edge list without them only
gets suggestions based on URL slugs.

## Sample output

```console
$ internal-link-graph analyze examples/sample-edges.csv --suggestions
Internal link graph: https://example.com/
========================================================================
Pages analysed            14   (URLs known: 14)
Indexable pages           14
Unique link edges         29   (internal links: 29, external: 0)
Average inlinks        2.071
Max click depth            4   (average 1.846)
Orphan pages               1
Unreachable pages          0
Dead ends                  1
Link equity lost       0.00%   broken 0.00%, redirect 0.00%, nofollow 0.00%
PageRank                  38 iterations
Status codes        not_crawled: 14

Top 10 pages by PageRank

 #  PageRank  In  Out  Depth  URL
--  --------  --  ---  -----  ------------------------------
 1   0.21202   4    4      0  /
 2   0.14398   4    1      1  /contact/
 3   0.12621   3    4      1  /services/
 4   0.12033   5    4      1  /blog/
 5   0.08203   4    2      2  /services/seo-audit/
 ...

Click depth distribution

    0       1  ######
    1       4  ########################
    2       5  ##############################
    3       2  ############
    4       1  ######

Issues: 12 (1 errors, 3 warnings, 8 notices)

Severity  Count  Issue
--------  -----  -----------------------------------------------
error         1  Orphan page (no internal inlinks)
warning       1  Page is many clicks away from the start URL
warning       1  Page without internal outlinks
warning       1  One anchor text dominates the inlinks of a page
notice        1  Generic anchor text
notice        7  Page with very few internal inlinks

Orphan pages (1)

  /landing/spring-offer/

Topic clusters (by URL directory)

Cluster     Pages  Internal  Out  In  Cohesion
----------  -----  --------  ---  --  --------
/blog/          7        11    3   1      0.79
/               3         5    2   4      0.71
/services/      3         4    3   4      0.57
/landing/       1         0    1   0      0.00

Link suggestions (1)

  /services/seo-audit/ -> /blog/technical-seo-checklist/
      anchor: "Technical seo checklist"  similarity 0.34
```

## How the metrics are computed

**The graph.** Every indexable-looking HTML page is a node. Repeated links between the same
two pages are one edge (the count is kept as a weight for the exports). Links to a
redirecting URL are attached to the redirect's final destination, and links to broken URLs
are dropped from the graph and reported. Nofollow links are excluded unless
`--include-nofollow` is given.

**PageRank.** Standard power iteration with damping factor `d` (default 0.85):

```text
PR(p) = (1 - d) / N  +  d * ( sum over q linking to p of PR(q) / outdegree(q)  +  D / N )
```

`D` is the total score of dangling pages (pages without outlinks), redistributed evenly so
the scores always sum to 1. Iteration stops when the total change falls below `1e-10`. With
`--personalized` a second run teleports only to the start URL, which measures how much
authority reaches a page *from the homepage* and gives zero to anything unreachable.

The value is internal only: it describes how your own links distribute importance, and knows
nothing about external backlinks. Compare pages with each other, not with any absolute scale;
`1 / N` is the average.

**Click depth.** Breadth-first search from the start URL over followed links. Depth 0 is the
start page. Pages at depth 4 or more (`--deep-threshold`) are flagged.

**Orphans, unreachable pages, dead ends.** An orphan has no followed internal inlinks at all
(typically discovered through the sitemap). An unreachable page has inlinks, but only from
pages that cannot be reached from the start URL themselves. A dead end is a page without
internal outlinks; after a crawl only pages that were actually fetched are counted.

**Strongly connected components.** Tarjan's algorithm, implemented iteratively so deep sites
do not hit the recursion limit. A healthy site is mostly one large component; small separate
components are sections users and crawlers can enter but not leave (or the reverse).

**Hubs and authorities.** HITS: a good hub links to many good authorities, a good authority is
linked from many good hubs. Category and index pages should score as hubs.

**Link equity lost.** For each page, the share of its outgoing PageRank that flows into links
which waste it - broken targets, redirects (counted fully, as a conservative upper bound) and
nofollow links - summed over the site and expressed as a share of total PageRank.

**Anchor text.** Anchors are grouped per target after lower-casing and whitespace
normalisation. A target with at least 5 inlinks where one anchor accounts for 80% or more is
reported as over-optimised; anchors such as "click here" or "read more" are reported as
generic.

**Topic clusters.** Pages are grouped by their first URL directory (`--cluster-depth` for
more levels). Cohesion is the share of the links starting in a cluster that stay inside it.

**Link suggestions.** Each page becomes a TF-IDF vector built from its title, H1, URL slug
(all weighted up) and body text. A suggestion links a source to a target when they are
similar, not linked yet, the target is deep (`--min-depth`) or has few inlinks
(`--max-inlinks`), and the source is indexable and not already overloaded with outlinks. The
score multiplies similarity by the source's relative PageRank and the target's need. The
proposed anchor is the target's H1, falling back to its cleaned title and then its URL slug.

## Use as a library

```python
from internal_link_graph import analyze, crawl, load_site, suggest_links

site = crawl("https://example.com/", max_pages=500, delay=0.5)   # or load_site("crawl.json")
result = analyze(site)

print(result.summary["orphans"], result.orphans)
for node in result.top_pages(10):
    print(f"{node.pagerank:.4f}  depth={node.depth}  {node.url}")
for issue in result.issues:
    print(issue.severity, issue.code, issue.url, issue.detail)
for suggestion in suggest_links(site, result):
    print(suggestion.source, "->", suggestion.target, f'"{suggestion.anchor}"')
```

The graph algorithms work on their own as well:

```python
from internal_link_graph import LinkGraph, pagerank

graph = LinkGraph()
graph.add_edge("a", "b")
graph.add_edge("b", "a")
result = pagerank(graph)
print(result.scores, result.iterations, result.converged)
```

Tests and dry runs can replace the network with an in-memory site:

```python
from internal_link_graph import DictFetcher, crawl

fetcher = DictFetcher({"https://example.com/": '<a href="/about">About</a>',
                       "https://example.com/about": "<h1>About</h1>"})
site = crawl("https://example.com/", fetcher=fetcher, delay=0)
```

## Limitations

- **No JavaScript rendering.** Only links present in the HTML the server returns are seen.
  Sites that build their navigation client-side will look far less connected than they are.
- PageRank is computed from internal links only; it is not Google's PageRank and ignores
  backlinks from other sites.
- All links on a page are weighted equally. Position (navigation, footer, content) is
  recorded and exported but does not change the scores.
- The whole graph is kept in memory. Sites up to a few hundred thousand pages are fine;
  beyond that, expect it to be slow.
- `--max-pages` counts fetched documents; the hops of a redirect chain are not counted, so
  a crawl can make slightly more requests than the limit.
- Redirect targets are followed even when they match an `--exclude` pattern, because the
  final status of a redirect is needed to judge the link pointing at it.
- Link suggestions are lexical (TF-IDF). They do not understand synonyms, and without
  `--unicode` only Latin-script words are tokenised.
- Near-duplicate title detection compares titles pairwise within buckets and is meant for
  catching templates, not for content deduplication.
- `robots.txt` support covers `User-agent`, `Allow`, `Disallow`, `Crawl-delay` and `Sitemap`
  as described above. Other directives (such as `Host` or `Clean-param`) are ignored, and
  the delay of the matching group applies to the whole host.

## Development

```console
$ pip install -e ".[dev]"
$ ruff check .
$ pytest
```

The test suite never touches the network. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT - see [LICENSE](LICENSE). Copyright (c) 2026 Saizul Amin.

## Docker image

A multi-arch image (linux/amd64, linux/arm64) is published to GitHub Packages:

```bash
docker pull ghcr.io/saizul/internal-link-graph:latest
docker run --rm -v "$PWD:/work" ghcr.io/saizul/internal-link-graph:latest --help
```

Mount the folder with your input files at `/work`; output files are written there too.
