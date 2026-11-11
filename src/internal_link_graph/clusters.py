"""Topic clusters from URL directories and near-duplicate title detection."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .graph import LinkGraph
from .textsim import split_unicode_words
from .urlnorm import path_segments, url_directory

ROOT_CLUSTER = "/"


@dataclass
class ClusterReport:
    """Directory based topic clusters and the links flowing between them."""

    depth: int = 1
    assignment: dict[str, str] = field(default_factory=dict)
    members: dict[str, list[str]] = field(default_factory=dict)
    matrix: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        """Cluster names, largest first."""
        return sorted(self.members, key=lambda name: (-len(self.members[name]), name))

    def links_between(self, source: str, target: str) -> int:
        return self.matrix.get(source, {}).get(target, 0)

    def internal_links(self, name: str) -> int:
        return self.links_between(name, name)

    def outgoing_links(self, name: str) -> int:
        return sum(count for other, count in self.matrix.get(name, {}).items() if other != name)

    def incoming_links(self, name: str) -> int:
        return sum(row.get(name, 0) for other, row in self.matrix.items() if other != name)

    def cohesion(self, name: str) -> float:
        """Share of a cluster's outgoing links that stay inside the cluster."""
        internal = self.internal_links(name)
        total = internal + self.outgoing_links(name)
        return internal / total if total else 0.0

    def isolated(self) -> list[str]:
        """Non-root clusters that receive no links from any other cluster."""
        return [
            name
            for name in self.names
            if name != ROOT_CLUSTER and self.incoming_links(name) == 0
        ]

    def to_dict(self) -> dict[str, object]:
        return {
            "depth": self.depth,
            "clusters": [
                {
                    "name": name,
                    "pages": len(self.members[name]),
                    "internal_links": self.internal_links(name),
                    "outgoing_links": self.outgoing_links(name),
                    "incoming_links": self.incoming_links(name),
                    "cohesion": round(self.cohesion(name), 4),
                }
                for name in self.names
            ],
            "matrix": {
                name: dict(sorted(row.items())) for name, row in sorted(self.matrix.items())
            },
        }


def topic_clusters(graph: LinkGraph, depth: int = 1) -> ClusterReport:
    """Assign every node to its URL directory and count cross-cluster links."""
    report = ClusterReport(depth=depth)
    names = {url: url_directory(url, depth) for url in graph.nodes}
    known = set(names.values())
    for url in graph.nodes:
        name = names[url]
        # A hub page such as ``/blog/`` (or ``/blog``) belongs to the directory
        # it introduces, provided other pages actually live in that directory.
        hub = _hub_directory(url, depth)
        if hub in known:
            name = hub
        report.assignment[url] = name
        report.members.setdefault(name, []).append(url)
    for name in report.members:
        report.matrix[name] = {}
    for source, target, _count in graph.edges():
        row = report.matrix[report.assignment[source]]
        column = report.assignment[target]
        row[column] = row.get(column, 0) + 1
    return report


def _hub_directory(url: str, depth: int) -> str:
    """Directory a URL would name if it were a section index page."""
    segments = path_segments(url)
    if not segments or depth < 1 or len(segments) > depth:
        return ""
    if "." in segments[-1]:
        return ""
    return "/" + "/".join(segments) + "/"


# --------------------------------------------------------------------------
# Near-duplicate titles
# --------------------------------------------------------------------------

_SEPARATOR_RE = re.compile(r"\s+[|\-–—·:»]+\s+")


def normalize_title(title: str) -> str:
    """Case-fold, drop punctuation and collapse whitespace."""
    decomposed = unicodedata.normalize("NFKC", title).casefold()
    return " ".join(split_unicode_words(decomposed))


def title_shingles(title: str, size: int = 3) -> frozenset[str]:
    """Character shingles of the normalised title (word level for short titles)."""
    normalised = normalize_title(title)
    if not normalised:
        return frozenset()
    if len(normalised) <= size:
        return frozenset({normalised})
    return frozenset(normalised[i : i + size] for i in range(len(normalised) - size + 1))


def jaccard(a: frozenset[str] | set[str], b: frozenset[str] | set[str]) -> float:
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    if intersection == 0:
        return 0.0
    return intersection / (len(a) + len(b) - intersection)


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            if root_a > root_b:
                root_a, root_b = root_b, root_a
            self.parent[root_b] = root_a


@dataclass
class TitleCluster:
    """Pages sharing an identical or nearly identical ``<title>``."""

    urls: list[str]
    titles: list[str]
    exact: bool

    @property
    def size(self) -> int:
        return len(self.urls)

    def to_dict(self) -> dict[str, object]:
        return {"exact": self.exact, "urls": self.urls, "titles": self.titles}


def strip_site_suffix(titles: Mapping[str, str], min_share: float = 0.5) -> dict[str, str]:
    """Remove a brand suffix/prefix (``"... | Example Shop"``) shared by most titles.

    Without this every title on a site looks similar to every other one.
    """
    if len(titles) < 3:
        return dict(titles)
    suffixes: dict[str, int] = {}
    prefixes: dict[str, int] = {}
    split: dict[str, list[str]] = {}
    for url, title in titles.items():
        parts = [part.strip() for part in _SEPARATOR_RE.split(title) if part.strip()]
        split[url] = parts
        if len(parts) > 1:
            suffixes[parts[-1]] = suffixes.get(parts[-1], 0) + 1
            prefixes[parts[0]] = prefixes.get(parts[0], 0) + 1
    threshold = max(2.0, len(titles) * min_share)
    brand_suffix = {part for part, count in suffixes.items() if count >= threshold}
    brand_prefix = {part for part, count in prefixes.items() if count >= threshold}
    result = {}
    for url, title in titles.items():
        parts = split[url]
        if len(parts) > 1 and parts[-1] in brand_suffix:
            parts = parts[:-1]
        elif len(parts) > 1 and parts[0] in brand_prefix:
            parts = parts[1:]
        else:
            result[url] = title
            continue
        result[url] = " - ".join(parts)
    return result


def duplicate_title_clusters(
    titles: Mapping[str, str],
    threshold: float = 0.85,
    strip_brand: bool = True,
    max_bucket: int = 200,
) -> list[TitleCluster]:
    """Cluster URLs whose titles are equal or near-duplicates.

    Similarity is the Jaccard index of character trigrams of the normalised
    titles.  Candidate pairs are found through a shingle -> pages inverted
    index so the comparison stays far below quadratic on normal sites;
    shingles shared by more than ``max_bucket`` pages are too common to be
    useful and are skipped.
    """
    usable = {url: title for url, title in titles.items() if title and title.strip()}
    if strip_brand:
        usable = strip_site_suffix(usable)
    urls = sorted(usable)
    normalised = [normalize_title(usable[url]) for url in urls]
    shingles = [title_shingles(usable[url]) for url in urls]
    union = _UnionFind(len(urls))

    by_exact: dict[str, int] = {}
    for position, text in enumerate(normalised):
        if not text:
            continue
        first = by_exact.setdefault(text, position)
        if first != position:
            union.union(first, position)

    if threshold < 1.0:
        buckets: dict[str, list[int]] = {}
        representatives = sorted(set(by_exact.values()))
        for position in representatives:
            for shingle in shingles[position]:
                buckets.setdefault(shingle, []).append(position)
        compared: set[tuple[int, int]] = set()
        for members in buckets.values():
            if len(members) < 2 or len(members) > max_bucket:
                continue
            for i, a in enumerate(members):
                for b in members[i + 1 :]:
                    if (a, b) in compared:
                        continue
                    compared.add((a, b))
                    if jaccard(shingles[a], shingles[b]) >= threshold:
                        union.union(a, b)

    groups: dict[int, list[int]] = {}
    for position, text in enumerate(normalised):
        if text:
            groups.setdefault(union.find(position), []).append(position)
    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        exact = len({normalised[position] for position in members}) == 1
        clusters.append(
            TitleCluster(
                urls=[urls[position] for position in members],
                titles=[titles[urls[position]] for position in members],
                exact=exact,
            )
        )
    clusters.sort(key=lambda cluster: (-cluster.size, cluster.urls[0]))
    return clusters


def cluster_sizes(clusters: Iterable[TitleCluster]) -> int:
    """Total number of pages involved in duplicate-title clusters."""
    return sum(cluster.size for cluster in clusters)
