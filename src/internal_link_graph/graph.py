"""A compact directed graph and the core link-analysis algorithms.

All algorithms are implemented here without third-party libraries:

* PageRank (power iteration, dangling-node redistribution, personalisation)
* HITS hubs and authorities
* breadth-first click depth
* strongly connected components (iterative Tarjan)
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .models import SiteData


class LinkGraph:
    """Directed multigraph over URL strings stored as integer adjacency maps.

    ``out[i]`` maps successor index -> number of parallel links; ``inn[i]`` is
    the mirror image.  Self-loops are dropped on insertion unless
    ``allow_self_loops`` is set, because a page linking to itself carries no
    navigational or ranking information.
    """

    def __init__(self, allow_self_loops: bool = False) -> None:
        self.nodes: list[str] = []
        self.index: dict[str, int] = {}
        self.out: list[dict[int, int]] = []
        self.inn: list[dict[int, int]] = []
        self.allow_self_loops = allow_self_loops

    # -- construction --------------------------------------------------------

    def add_node(self, url: str) -> int:
        idx = self.index.get(url)
        if idx is None:
            idx = len(self.nodes)
            self.index[url] = idx
            self.nodes.append(url)
            self.out.append({})
            self.inn.append({})
        return idx

    def add_edge(self, source: str, target: str, count: int = 1) -> bool:
        """Add ``count`` parallel links; returns False for a rejected self-loop."""
        s = self.add_node(source)
        t = self.add_node(target)
        if s == t and not self.allow_self_loops:
            return False
        self.out[s][t] = self.out[s].get(t, 0) + count
        self.inn[t][s] = self.inn[t].get(s, 0) + count
        return True

    @classmethod
    def from_edges(
        cls,
        edges: Iterable[tuple[str, str]],
        nodes: Iterable[str] = (),
        allow_self_loops: bool = False,
    ) -> LinkGraph:
        graph = cls(allow_self_loops=allow_self_loops)
        for node in nodes:
            graph.add_node(node)
        for source, target in edges:
            graph.add_edge(source, target)
        return graph

    @classmethod
    def from_site(
        cls,
        site: SiteData,
        follow_only: bool = True,
        resolve_redirects: bool = True,
        include_broken: bool = False,
    ) -> LinkGraph:
        """Build the internal link graph of a crawled or imported site.

        * ``follow_only`` - ignore links flagged nofollow / ugc / sponsored.
        * ``resolve_redirects`` - a link to a redirecting URL counts as a link
          to the final destination, and redirecting URLs are not nodes.
        * ``include_broken`` - keep 4xx/5xx URLs as nodes (off by default; a
          link to a broken page is reported as lost equity instead).

        Nodes are inserted in sorted URL order, which makes every downstream
        result deterministic.
        """
        graph = cls()

        def keep(url: str) -> bool:
            page = site.pages.get(url)
            if page is None:
                return True
            if page.is_redirect and resolve_redirects:
                return False
            return include_broken or not page.is_broken

        def resolve(url: str) -> str:
            return site.resolve_redirect(url) if resolve_redirects else url

        candidates = set(site.pages)
        candidates.update(resolve(url) for url in site.sitemap_urls)
        if site.start_url:
            candidates.add(resolve(site.start_url))
        for url in sorted(candidates):
            if keep(url):
                graph.add_node(url)
        for link in site.links:
            if not link.internal or (follow_only and link.nofollow):
                continue
            source = link.source
            target = resolve(link.target)
            if source not in graph.index:
                if not keep(source):
                    continue
                graph.add_node(source)
            if target not in graph.index:
                if not keep(target):
                    continue
                graph.add_node(target)
            graph.add_edge(source, target)
        return graph

    # -- accessors -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self.nodes)

    def __contains__(self, url: object) -> bool:
        return url in self.index

    @property
    def edge_count(self) -> int:
        """Number of distinct (source, target) pairs."""
        return sum(len(targets) for targets in self.out)

    @property
    def link_count(self) -> int:
        """Number of links counting parallel links separately."""
        return sum(sum(targets.values()) for targets in self.out)

    def has_edge(self, source: str, target: str) -> bool:
        s = self.index.get(source)
        t = self.index.get(target)
        return s is not None and t is not None and t in self.out[s]

    def successors(self, url: str) -> list[str]:
        idx = self.index.get(url)
        return [] if idx is None else [self.nodes[t] for t in self.out[idx]]

    def predecessors(self, url: str) -> list[str]:
        idx = self.index.get(url)
        return [] if idx is None else [self.nodes[s] for s in self.inn[idx]]

    def out_degree(self, url: str) -> int:
        idx = self.index.get(url)
        return 0 if idx is None else len(self.out[idx])

    def in_degree(self, url: str) -> int:
        idx = self.index.get(url)
        return 0 if idx is None else len(self.inn[idx])

    def edges(self) -> list[tuple[str, str, int]]:
        """All edges as ``(source, target, multiplicity)`` in insertion order."""
        result = []
        for s, targets in enumerate(self.out):
            for t, count in targets.items():
                result.append((self.nodes[s], self.nodes[t], count))
        return result

    def density(self) -> float:
        n = len(self.nodes)
        if n < 2:
            return 0.0
        return self.edge_count / (n * (n - 1))

    def reciprocity(self) -> float:
        """Share of edges whose reverse edge also exists."""
        total = self.edge_count
        if total == 0:
            return 0.0
        mutual = sum(1 for s, targets in enumerate(self.out) for t in targets if s in self.out[t])
        return mutual / total


# --------------------------------------------------------------------------
# PageRank
# --------------------------------------------------------------------------


@dataclass
class PageRankResult:
    """Scores (summing to 1) plus convergence diagnostics."""

    scores: dict[str, float] = field(default_factory=dict)
    iterations: int = 0
    converged: bool = True
    error: float = 0.0

    def top(self, limit: int = 10) -> list[tuple[str, float]]:
        ranked = sorted(self.scores.items(), key=lambda item: (-item[1], item[0]))
        return ranked[:limit]


def pagerank(
    graph: LinkGraph,
    damping: float = 0.85,
    tolerance: float = 1e-10,
    max_iterations: int = 200,
    personalization: Mapping[str, float] | None = None,
    weighted: bool = False,
) -> PageRankResult:
    """PageRank by power iteration.

    For a graph with ``N`` nodes, damping ``d`` and teleport distribution ``v``
    (uniform unless ``personalization`` is given)::

        PR(p) = (1 - d) * v(p) + d * ( sum_{q -> p} PR(q) / L(q) + D * v(p) )

    where ``L(q)`` is the out-degree of ``q`` and ``D`` is the total rank held
    by dangling nodes (pages without outlinks), which is redistributed
    according to ``v`` so that no rank leaks out of the system.

    Iteration stops when the L1 change between two rounds drops below
    ``tolerance`` or after ``max_iterations`` rounds.  With ``weighted`` the
    rank of a page is split proportionally to the number of parallel links.
    """
    if not 0.0 <= damping <= 1.0:
        raise ValueError("damping must be between 0 and 1")
    n = len(graph)
    if n == 0:
        return PageRankResult()

    if personalization:
        teleport = [0.0] * n
        for url, weight in personalization.items():
            idx = graph.index.get(url)
            if idx is not None and weight > 0:
                teleport[idx] = float(weight)
        total = sum(teleport)
        if total <= 0:
            raise ValueError("personalization has no positive weight on a graph node")
        teleport = [value / total for value in teleport]
    else:
        teleport = [1.0 / n] * n

    if weighted:
        out_weight = [float(sum(targets.values())) for targets in graph.out]
    else:
        out_weight = [float(len(targets)) for targets in graph.out]
    dangling = [i for i in range(n) if out_weight[i] == 0]

    rank = list(teleport)
    iterations = 0
    error = 0.0
    converged = False
    for iterations in range(1, max_iterations + 1):  # noqa: B007 - value used after the loop
        dangling_mass = sum(rank[i] for i in dangling)
        base = 1.0 - damping + damping * dangling_mass
        new_rank = [base * teleport[i] for i in range(n)]
        for source in range(n):
            weight = out_weight[source]
            if weight == 0:
                continue
            share = damping * rank[source] / weight
            if weighted:
                for target, count in graph.out[source].items():
                    new_rank[target] += share * count
            else:
                for target in graph.out[source]:
                    new_rank[target] += share
        error = sum(abs(new_rank[i] - rank[i]) for i in range(n))
        rank = new_rank
        if error < tolerance:
            converged = True
            break
    total = sum(rank)
    if total > 0:
        rank = [value / total for value in rank]
    return PageRankResult(
        scores={graph.nodes[i]: rank[i] for i in range(n)},
        iterations=iterations,
        converged=converged,
        error=error,
    )


# --------------------------------------------------------------------------
# HITS
# --------------------------------------------------------------------------


@dataclass
class HitsResult:
    hubs: dict[str, float] = field(default_factory=dict)
    authorities: dict[str, float] = field(default_factory=dict)
    iterations: int = 0
    converged: bool = True


def hits(graph: LinkGraph, tolerance: float = 1e-10, max_iterations: int = 200) -> HitsResult:
    """Kleinberg's HITS algorithm.

    A good *hub* links to many good authorities; a good *authority* is linked
    from many good hubs.  Both score vectors are L2-normalised each round.
    """
    n = len(graph)
    if n == 0:
        return HitsResult()
    if graph.edge_count == 0:
        zeros = dict.fromkeys(graph.nodes, 0.0)
        return HitsResult(hubs=dict(zeros), authorities=dict(zeros))

    def normalise(values: list[float]) -> list[float]:
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values] if norm > 0 else values

    hubs = [1.0 / math.sqrt(n)] * n
    authorities = [0.0] * n
    converged = False
    iterations = 0
    for iterations in range(1, max_iterations + 1):  # noqa: B007 - value used after the loop
        new_auth = normalise([sum(hubs[s] for s in graph.inn[i]) for i in range(n)])
        new_hubs = normalise([sum(new_auth[t] for t in graph.out[i]) for i in range(n)])
        change = sum(abs(new_hubs[i] - hubs[i]) for i in range(n)) + sum(
            abs(new_auth[i] - authorities[i]) for i in range(n)
        )
        hubs, authorities = new_hubs, new_auth
        if change < tolerance:
            converged = True
            break
    return HitsResult(
        hubs={graph.nodes[i]: hubs[i] for i in range(n)},
        authorities={graph.nodes[i]: authorities[i] for i in range(n)},
        iterations=iterations,
        converged=converged,
    )


# --------------------------------------------------------------------------
# Traversal
# --------------------------------------------------------------------------


def click_depth(graph: LinkGraph, start: str) -> dict[str, int]:
    """Minimum number of clicks from ``start`` to every reachable page (BFS)."""
    origin = graph.index.get(start)
    if origin is None:
        return {}
    depth = {origin: 0}
    queue = deque([origin])
    while queue:
        current = queue.popleft()
        next_depth = depth[current] + 1
        for target in graph.out[current]:
            if target not in depth:
                depth[target] = next_depth
                queue.append(target)
    return {graph.nodes[idx]: value for idx, value in depth.items()}


def shortest_path(graph: LinkGraph, start: str, goal: str) -> list[str]:
    """One shortest click path from ``start`` to ``goal`` (empty if unreachable)."""
    origin = graph.index.get(start)
    destination = graph.index.get(goal)
    if origin is None or destination is None:
        return []
    parent: dict[int, int | None] = {origin: None}
    queue = deque([origin])
    while queue:
        current = queue.popleft()
        if current == destination:
            break
        for target in graph.out[current]:
            if target not in parent:
                parent[target] = current
                queue.append(target)
    if destination not in parent:
        return []
    path: list[str] = []
    cursor: int | None = destination
    while cursor is not None:
        path.append(graph.nodes[cursor])
        cursor = parent[cursor]
    path.reverse()
    return path


def strongly_connected_components(graph: LinkGraph) -> list[list[str]]:
    """Strongly connected components via an iterative version of Tarjan's algorithm.

    The recursion of the textbook algorithm is replaced by an explicit stack,
    so deep sites (long pagination chains) cannot overflow Python's call
    stack.  Components are returned largest first; ties are ordered by the
    smallest URL they contain, and URLs inside a component are sorted.
    """
    n = len(graph)
    index_of = [-1] * n
    lowlink = [0] * n
    on_stack = [False] * n
    stack: list[int] = []
    components: list[list[str]] = []
    counter = 0

    for root in range(n):
        if index_of[root] != -1:
            continue
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        work = [(root, iter(graph.out[root]))]
        while work:
            node, successors = work[-1]
            advanced = False
            for target in successors:
                if index_of[target] == -1:
                    index_of[target] = lowlink[target] = counter
                    counter += 1
                    stack.append(target)
                    on_stack[target] = True
                    work.append((target, iter(graph.out[target])))
                    advanced = True
                    break
                if on_stack[target]:
                    lowlink[node] = min(lowlink[node], index_of[target])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component.append(graph.nodes[member])
                    if member == node:
                        break
                components.append(sorted(component))
    components.sort(key=lambda component: (-len(component), component[0]))
    return components


def dead_ends(graph: LinkGraph) -> list[str]:
    """Nodes without any outgoing edge."""
    return [graph.nodes[i] for i in range(len(graph)) if not graph.out[i]]


def sources(graph: LinkGraph) -> list[str]:
    """Nodes without any incoming edge."""
    return [graph.nodes[i] for i in range(len(graph)) if not graph.inn[i]]
