"""Estimate how much link equity leaks through wasteful internal links.

Model: a page with PageRank ``p`` and ``k`` distinct internal link targets
passes ``d * p / k`` through each of them (``d`` is the damping factor).  A
link is considered wasteful when it

* points at a broken URL (4xx / 5xx / fetch error) - the share is lost,
* points at a redirect - the share arrives late and wastes crawl budget,
* is flagged ``nofollow`` / ``ugc`` / ``sponsored`` - the share evaporates.

The numbers are estimates meant for prioritisation, not exact science.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .models import SiteData

LOSS_KINDS = ("broken", "redirect", "nofollow")


@dataclass
class EquityLoss:
    """One wasteful (source, target) pair."""

    source: str
    target: str
    kind: str
    amount: float
    status: int = 0
    final_target: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "amount": self.amount,
            "status": self.status,
            "final_target": self.final_target,
        }


@dataclass
class EquityReport:
    """Aggregated equity loss."""

    losses: list[EquityLoss] = field(default_factory=list)
    totals: dict[str, float] = field(default_factory=lambda: dict.fromkeys(LOSS_KINDS, 0.0))
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(LOSS_KINDS, 0))
    by_source: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return sum(self.totals.values())

    def top(self, limit: int = 20, kind: str | None = None) -> list[EquityLoss]:
        items = [loss for loss in self.losses if kind is None or loss.kind == kind]
        return items[:limit]

    def to_dict(self, limit: int | None = None) -> dict[str, object]:
        losses = self.losses if limit is None else self.losses[:limit]
        return {
            "total": self.total,
            "totals": dict(self.totals),
            "counts": dict(self.counts),
            "losses": [loss.to_dict() for loss in losses],
        }


def link_equity_loss(
    site: SiteData,
    pagerank_scores: Mapping[str, float],
    damping: float = 0.85,
) -> EquityReport:
    """Compute the :class:`EquityReport` for ``site`` given PageRank scores."""
    # Group distinct internal targets per source; a pair is "nofollow" only if
    # every parallel link between the two pages is nofollow.
    targets_by_source: dict[str, dict[str, bool]] = {}
    for link in site.links:
        if not link.internal or link.source == link.target:
            continue
        targets = targets_by_source.setdefault(link.source, {})
        targets[link.target] = targets.get(link.target, True) and link.nofollow

    report = EquityReport()
    for source in sorted(targets_by_source):
        rank = pagerank_scores.get(source, 0.0)
        targets = targets_by_source[source]
        if rank <= 0 or not targets:
            continue
        share = damping * rank / len(targets)
        for target in sorted(targets):
            page = site.pages.get(target)
            kind = ""
            final_target = ""
            if targets[target]:
                kind = "nofollow"
            elif page is not None and page.is_broken:
                kind = "broken"
            elif page is not None and page.is_redirect:
                final_target = site.resolve_redirect(target)
                final_page = site.pages.get(final_target)
                # A redirect that ends on a broken page loses everything.
                kind = "broken" if final_page is not None and final_page.is_broken else "redirect"
            if not kind:
                continue
            report.losses.append(
                EquityLoss(
                    source=source,
                    target=target,
                    kind=kind,
                    amount=share,
                    status=page.status if page is not None else 0,
                    final_target=final_target,
                )
            )
            report.totals[kind] += share
            report.counts[kind] += 1
            report.by_source[source] = report.by_source.get(source, 0.0) + share
    report.losses.sort(key=lambda loss: (-loss.amount, loss.source, loss.target))
    return report
