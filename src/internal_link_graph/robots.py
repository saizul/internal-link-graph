"""robots.txt handling with a per-origin cache."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from urllib.robotparser import RobotFileParser

from .fetcher import Fetcher, fetch_following
from .html_extract import decode_body
from .urlnorm import origin_of


@dataclass
class RobotsRules:
    """Parsed robots.txt of one origin."""

    parser: RobotFileParser | None = None
    allow_all: bool = False
    disallow_all: bool = False
    sitemaps: list[str] = field(default_factory=list)
    status: int = 0

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all or self.parser is None:
            return True
        return self.parser.can_fetch(user_agent, url)

    def crawl_delay(self, user_agent: str) -> float | None:
        if self.parser is None:
            return None
        try:
            delay = self.parser.crawl_delay(user_agent)
        except (AttributeError, ValueError):
            return None
        return float(delay) if delay is not None else None


def parse_robots(text: str, status: int = 200) -> RobotsRules:
    """Build :class:`RobotsRules` from the text of a robots.txt file."""
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    sitemaps = list(parser.site_maps() or [])
    return RobotsRules(parser=parser, sitemaps=sitemaps, status=status)


def rules_for_response(status: int, body: bytes, content_type: str = "") -> RobotsRules:
    """Translate an HTTP response for ``/robots.txt`` into rules.

    Following the conventions used by the major search engines:

    * 2xx - parse the file,
    * 401 / 403 - the whole site is off limits,
    * other 4xx - no restrictions,
    * 5xx or network errors - be conservative and do not crawl.
    """
    if 200 <= status < 300:
        return parse_robots(decode_body(body, content_type), status=status)
    if status in (401, 403):
        return RobotsRules(disallow_all=True, status=status)
    if 400 <= status < 500:
        return RobotsRules(allow_all=True, status=status)
    return RobotsRules(disallow_all=True, status=status)


class RobotsCache:
    """Fetches and caches robots.txt per origin (thread-safe)."""

    def __init__(self, fetcher: Fetcher, user_agent: str, enabled: bool = True) -> None:
        self.fetcher = fetcher
        self.user_agent = user_agent
        self.enabled = enabled
        self._rules: dict[str, RobotsRules] = {}
        self._lock = threading.Lock()

    def rules(self, url: str) -> RobotsRules:
        origin = origin_of(url)
        with self._lock:
            cached = self._rules.get(origin)
            if cached is not None:
                return cached
            try:
                response, _ = fetch_following(self.fetcher, origin + "/robots.txt")
                rules = rules_for_response(
                    response.status, response.body, response.header("content-type")
                )
            except Exception:  # a broken fetcher must not take the crawl down
                rules = RobotsRules(disallow_all=True)
            self._rules[origin] = rules
            return rules

    def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        return self.rules(url).can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        if not self.enabled:
            return None
        return self.rules(url).crawl_delay(self.user_agent)

    def sitemaps(self, url: str) -> list[str]:
        """Sitemap URLs advertised in robots.txt (available even when disabled)."""
        return list(self.rules(url).sitemaps)
