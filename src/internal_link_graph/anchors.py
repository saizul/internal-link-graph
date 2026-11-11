"""Anchor text analysis: distribution per target, generic and over-optimised anchors."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import Link

#: Anchors that say nothing about the destination.
GENERIC_ANCHORS = frozenset(
    {
        "click here",
        "click",
        "here",
        "read more",
        "read more...",
        "read on",
        "more",
        "more info",
        "more information",
        "learn more",
        "find out more",
        "see more",
        "view more",
        "show more",
        "details",
        "view details",
        "more details",
        "this",
        "this page",
        "this link",
        "this article",
        "this post",
        "link",
        "website",
        "page",
        "go",
        "continue",
        "continue reading",
        "continue reading...",
        "check it out",
        "check this out",
        "visit",
        "visit site",
        "view",
        "see here",
        "download",
        "next",
        "previous",
        "আরও পড়ুন",
        "বিস্তারিত",
        "এখানে ক্লিক করুন",
        "এখানে",
    }
)

_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_RE = re.compile(r"[\s»›→>.:…!]+$")
_LEADING_RE = re.compile(r"^[\s«‹←<]+")


def normalize_anchor(text: str) -> str:
    """Lower-case, collapse whitespace and trim decorative arrows / punctuation."""
    text = _WHITESPACE_RE.sub(" ", text).strip().casefold()
    text = _TRAILING_RE.sub("", text)
    return _LEADING_RE.sub("", text)


def is_generic_anchor(text: str, extra: Iterable[str] = ()) -> bool:
    """True for anchors such as "click here" or "read more"."""
    normalised = normalize_anchor(text)
    if not normalised:
        return False
    return normalised in GENERIC_ANCHORS or normalised in {normalize_anchor(e) for e in extra}


@dataclass
class AnchorProfile:
    """Anchor text statistics for one link target."""

    target: str
    total: int = 0
    anchors: Counter[str] = field(default_factory=Counter)
    empty: int = 0
    generic: int = 0
    image: int = 0
    sources: set[str] = field(default_factory=set)

    @property
    def unique(self) -> int:
        return len(self.anchors)

    @property
    def top_anchor(self) -> str:
        if not self.anchors:
            return ""
        return min(self.anchors.items(), key=lambda item: (-item[1], item[0]))[0]

    @property
    def top_share(self) -> float:
        """Share of all inlinks (with text) that use the most common anchor."""
        with_text = self.total - self.empty
        if with_text <= 0:
            return 0.0
        return self.anchors[self.top_anchor] / with_text

    @property
    def diversity(self) -> float:
        """Distinct anchors divided by links with text (1.0 = all different)."""
        with_text = self.total - self.empty
        return self.unique / with_text if with_text > 0 else 0.0

    def distribution(self, limit: int | None = None) -> list[tuple[str, int, float]]:
        """``(anchor, count, share)`` rows, most common first."""
        with_text = max(self.total - self.empty, 1)
        ranked = sorted(self.anchors.items(), key=lambda item: (-item[1], item[0]))
        if limit is not None:
            ranked = ranked[:limit]
        return [(anchor, count, count / with_text) for anchor, count in ranked]

    def to_dict(self, limit: int = 10) -> dict[str, object]:
        return {
            "target": self.target,
            "total": self.total,
            "unique": self.unique,
            "empty": self.empty,
            "generic": self.generic,
            "image": self.image,
            "linking_pages": len(self.sources),
            "top_anchor": self.top_anchor,
            "top_share": round(self.top_share, 4),
            "anchors": [
                {"anchor": anchor, "count": count, "share": round(share, 4)}
                for anchor, count, share in self.distribution(limit)
            ],
        }


def anchor_profiles(
    links: Iterable[Link],
    resolve: dict[str, str] | None = None,
    extra_generic: Iterable[str] = (),
) -> dict[str, AnchorProfile]:
    """Group internal links by target and summarise their anchor texts.

    ``resolve`` optionally maps redirecting URLs to their final destination so
    anchors are attributed to the page that actually ranks.
    """
    extra = {normalize_anchor(e) for e in extra_generic}
    profiles: dict[str, AnchorProfile] = {}
    for link in links:
        if not link.internal or link.source == link.target:
            continue
        target = resolve.get(link.target, link.target) if resolve else link.target
        profile = profiles.get(target)
        if profile is None:
            profile = profiles[target] = AnchorProfile(target=target)
        profile.total += 1
        profile.sources.add(link.source)
        if link.is_image:
            profile.image += 1
        anchor = normalize_anchor(link.anchor)
        if not anchor:
            profile.empty += 1
            continue
        profile.anchors[anchor] += 1
        if anchor in GENERIC_ANCHORS or anchor in extra:
            profile.generic += 1
    return profiles


def over_optimized(
    profiles: dict[str, AnchorProfile],
    min_links: int = 5,
    dominance: float = 0.8,
    min_sources: int = 3,
) -> list[AnchorProfile]:
    """Targets where one non-generic anchor dominates suspiciously.

    A profile is flagged when it has at least ``min_links`` text links from at
    least ``min_sources`` different pages and a single anchor accounts for
    ``dominance`` or more of them.  Site-wide navigation legitimately repeats
    one anchor, so this is a hint to review, not an error.
    """
    flagged = []
    for profile in profiles.values():
        with_text = profile.total - profile.empty
        if with_text < min_links or len(profile.sources) < min_sources:
            continue
        top = profile.top_anchor
        if not top or top in GENERIC_ANCHORS:
            continue
        if profile.top_share >= dominance:
            flagged.append(profile)
    flagged.sort(key=lambda profile: (-profile.top_share, -profile.total, profile.target))
    return flagged
