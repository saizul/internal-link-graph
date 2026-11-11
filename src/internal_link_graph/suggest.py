"""Internal link suggestions.

Idea: strong pages (high PageRank) should lend a hand to pages that are
buried deep in the site or have very few inlinks - but only when the two
pages are actually about related things.  Relatedness is the cosine
similarity of TF-IDF vectors built from title, H1, URL slug and body text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from .analyzer import AnalysisConfig, AnalysisResult, analyze
from .models import Page, SiteData
from .textsim import DEFAULT_STOPWORDS, TfidfIndex, tokenize
from .urlnorm import path_segments

_TITLE_SEPARATOR_RE = re.compile(r"\s+[|\-–—·»:]+\s+")
_SLUG_SPLIT_RE = re.compile(r"[-_+.\s]+")
_FILE_EXTENSION_RE = re.compile(r"\.(html?|php|aspx?|jsp)$", re.IGNORECASE)


@dataclass
class SuggestConfig:
    """Tuning knobs for :func:`suggest_links`."""

    max_suggestions: int = 50
    per_target: int = 3
    per_source: int = 5
    min_similarity: float = 0.1
    min_depth: int = 3
    max_inlinks: int = 2
    source_pool: int = 50
    max_source_outlinks: int = 150
    unicode_tokens: bool = False
    title_weight: int = 3
    h1_weight: int = 2
    slug_weight: int = 2
    max_anchor_words: int = 8


@dataclass
class Suggestion:
    """A proposed new link ``source -> target``."""

    source: str
    target: str
    score: float
    similarity: float
    source_pagerank: float
    target_depth: int | None
    target_inlinks: int
    anchor: str
    shared_terms: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "target": self.target,
            "score": round(self.score, 6),
            "similarity": round(self.similarity, 6),
            "source_pagerank": self.source_pagerank,
            "target_depth": self.target_depth,
            "target_inlinks": self.target_inlinks,
            "anchor": self.anchor,
            "shared_terms": list(self.shared_terms),
            "reason": self.reason,
        }


def slug_words(url: str) -> str:
    """Human readable words from the last path segment of a URL."""
    segments = path_segments(url)
    if not segments:
        return ""
    slug = _FILE_EXTENSION_RE.sub("", unquote(segments[-1]))
    return " ".join(part for part in _SLUG_SPLIT_RE.split(slug) if part)


def clean_title(title: str) -> str:
    """Drop the site name from ``"Page title | Site"`` style titles."""
    parts = [part.strip() for part in _TITLE_SEPARATOR_RE.split(title) if part.strip()]
    if not parts:
        return title.strip()
    if len(parts) == 1:
        return parts[0]
    # The page-specific part is nearly always the longest one.
    return max(parts[:2], key=len) if len(parts[0]) < 4 else parts[0]


def suggest_anchor(page: Page | None, url: str, max_words: int = 8) -> str:
    """Anchor text proposal: H1, else cleaned title, else words from the slug."""
    candidates = []
    if page is not None:
        candidates = [page.h1.strip(), clean_title(page.title)]
    candidates.append(slug_words(url).capitalize())
    for candidate in candidates:
        if candidate:
            words = candidate.split()
            if len(words) > max_words:
                candidate = " ".join(words[:max_words])
            return candidate.rstrip(" ,;:-")
    return url


def document_tokens(page: Page | None, url: str, config: SuggestConfig) -> list[str]:
    """Weighted token list for one page (title and H1 count multiple times)."""

    def tokens(text: str) -> list[str]:
        return tokenize(text, unicode_letters=config.unicode_tokens, stopwords=DEFAULT_STOPWORDS)

    result: list[str] = []
    result.extend(tokens(slug_words(url)) * config.slug_weight)
    if page is not None:
        result.extend(tokens(clean_title(page.title)) * config.title_weight)
        result.extend(tokens(page.h1) * config.h1_weight)
        result.extend(tokens(page.text))
    return result


def build_index(site: SiteData, urls: list[str], config: SuggestConfig) -> TfidfIndex:
    """TF-IDF index over the given URLs."""
    documents = {url: document_tokens(site.pages.get(url), url, config) for url in urls}
    return TfidfIndex(documents)


def _eligible(page: Page | None) -> bool:
    if page is None:
        return True
    if page.blocked_by_robots or not page.indexable:
        return False
    return not page.content_type or "html" in page.content_type


def suggest_links(
    site: SiteData,
    analysis: AnalysisResult | None = None,
    config: SuggestConfig | None = None,
) -> list[Suggestion]:
    """Propose new internal links, best first.

    * **targets** are indexable pages that are at least ``min_depth`` clicks
      deep, unreachable, or have at most ``max_inlinks`` linking pages;
    * **sources** are the ``source_pool`` pages with the highest PageRank that
      are not already overloaded with outlinks;
    * pairs that are already linked (in the source -> target direction,
      follow or nofollow) are excluded;
    * ``score = similarity * (0.5 + 0.5 * PR(source) / max PR) * need`` where
      ``need`` grows with the depth of the target and shrinks with its
      number of inlinks.
    """
    config = config or SuggestConfig()
    if analysis is None:
        analysis = analyze(site, AnalysisConfig())
    nodes = analysis.nodes
    start = analysis.start_url
    eligible = [url for url in nodes if _eligible(site.pages.get(url))]
    if len(eligible) < 2:
        return []

    targets = []
    for url in eligible:
        node = nodes[url]
        if url == start:
            continue
        deep = node.depth is None or node.depth >= config.min_depth
        if deep or node.in_degree <= config.max_inlinks:
            targets.append(url)
    ranked_sources = sorted(eligible, key=lambda url: (-nodes[url].pagerank, url))
    sources = [
        url
        for url in ranked_sources
        if nodes[url].outlinks + nodes[url].external_outlinks < config.max_source_outlinks
    ][: config.source_pool]
    if not targets or not sources:
        return []

    index = build_index(site, sorted(set(targets) | set(sources)), config)
    source_set = frozenset(sources)
    max_rank = max((nodes[url].pagerank for url in sources), default=0.0) or 1.0
    existing = {(link.source, site.resolve_redirect(link.target)) for link in site.internal_links()}
    existing |= {(link.source, link.target) for link in site.internal_links()}

    candidates: list[Suggestion] = []
    for target in targets:
        node = nodes[target]
        similar = index.most_similar(
            target, candidates=source_set, limit=0, min_similarity=config.min_similarity
        )
        depth_factor = 1.0 + 0.1 * min(node.depth if node.depth is not None else 10, 10)
        inlink_factor = 1.0 / (1.0 + 0.25 * node.in_degree)
        need = depth_factor * (0.5 + 0.5 * inlink_factor)
        for source, similarity in similar:
            if source == target or (source, target) in existing:
                continue
            source_rank = nodes[source].pagerank
            score = similarity * (0.5 + 0.5 * source_rank / max_rank) * need
            candidates.append(
                Suggestion(
                    source=source,
                    target=target,
                    score=score,
                    similarity=similarity,
                    source_pagerank=source_rank,
                    target_depth=node.depth,
                    target_inlinks=node.in_degree,
                    anchor=suggest_anchor(site.pages.get(target), target, config.max_anchor_words),
                )
            )
    candidates.sort(key=lambda s: (-s.score, s.target, s.source))

    per_target: dict[str, int] = {}
    per_source: dict[str, int] = {}
    chosen: list[Suggestion] = []
    for suggestion in candidates:
        if len(chosen) >= config.max_suggestions:
            break
        if per_target.get(suggestion.target, 0) >= config.per_target:
            continue
        if per_source.get(suggestion.source, 0) >= config.per_source:
            continue
        per_target[suggestion.target] = per_target.get(suggestion.target, 0) + 1
        per_source[suggestion.source] = per_source.get(suggestion.source, 0) + 1
        suggestion.shared_terms = index.shared_terms(suggestion.source, suggestion.target)
        suggestion.reason = _reason(suggestion)
        chosen.append(suggestion)
    return chosen


def _reason(suggestion: Suggestion) -> str:
    if suggestion.target_depth is None:
        where = "is not reachable from the start page"
    elif suggestion.target_inlinks == 0:
        where = "has no internal inlinks"
    else:
        plural = "s" if suggestion.target_inlinks != 1 else ""
        where = (
            f"is {suggestion.target_depth} click(s) deep with "
            f"{suggestion.target_inlinks} linking page{plural}"
        )
    topic = ", ".join(suggestion.shared_terms[:3])
    related = f"; shared topic: {topic}" if topic else ""
    return f"target {where}{related}"
