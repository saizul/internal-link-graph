"""Crawl a website, build its internal link graph and report SEO problems."""

__version__ = "0.1.0"

from .analyzer import (  # noqa: E402
    AnalysisConfig,
    AnalysisResult,
    Issue,
    NodeMetrics,
    analyze,
)
from .crawler import CrawlConfig, Crawler, CrawlError, crawl  # noqa: E402
from .diff import CrawlDiff, diff_analyses, diff_sites  # noqa: E402
from .fetcher import DictFetcher, Fetcher, Response, UrllibFetcher  # noqa: E402
from .graph import (  # noqa: E402
    LinkGraph,
    click_depth,
    hits,
    pagerank,
    shortest_path,
    strongly_connected_components,
)
from .importers import load_site  # noqa: E402
from .models import Link, Page, SiteData  # noqa: E402
from .suggest import SuggestConfig, Suggestion, suggest_links  # noqa: E402
from .urlnorm import NormalizeOptions, normalize_url  # noqa: E402

__all__ = [
    "AnalysisConfig",
    "AnalysisResult",
    "CrawlConfig",
    "CrawlDiff",
    "CrawlError",
    "Crawler",
    "DictFetcher",
    "Fetcher",
    "Issue",
    "Link",
    "LinkGraph",
    "NodeMetrics",
    "NormalizeOptions",
    "Page",
    "Response",
    "SiteData",
    "SuggestConfig",
    "Suggestion",
    "UrllibFetcher",
    "__version__",
    "analyze",
    "click_depth",
    "crawl",
    "diff_analyses",
    "diff_sites",
    "hits",
    "load_site",
    "normalize_url",
    "pagerank",
    "shortest_path",
    "strongly_connected_components",
    "suggest_links",
]
