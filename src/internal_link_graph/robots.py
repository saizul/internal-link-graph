"""robots.txt handling with a per-origin cache.

The parser follows RFC 9309 (the Robots Exclusion Protocol) rather than the
first-match semantics of :mod:`urllib.robotparser`:

* user agents are matched on their product token, case-insensitively; groups
  naming the same agent are merged and ``*`` is only used when no group names
  the crawler,
* ``*`` in a path matches any sequence of characters and a trailing ``$``
  anchors the pattern at the end of the URL,
* the most specific (longest) matching rule wins; on a tie ``Allow`` wins,
* percent-encoding is normalised on both sides before comparing, so
  ``/caf%C3%A9`` and ``/café`` are the same path,
* ``/robots.txt`` itself is always allowed and only the first 500 KiB of the
  file are parsed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .fetcher import Fetcher, fetch_following
from .html_extract import decode_body
from .urlnorm import origin_of

MAX_ROBOTS_BYTES = 500 * 1024
"""Parsing limit required by RFC 9309; anything after it is ignored."""

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = frozenset("0123456789abcdefABCDEF")
_AGENT_KEYS = frozenset({"user-agent", "useragent", "user agent"})
_RULE_KEYS = {"allow": True, "disallow": False}


def _encode_path(text: str) -> str:
    """Normalise percent-encoding so equivalent paths compare equal.

    Escapes of unreserved characters are decoded, other escapes are upper-cased
    and characters outside printable US-ASCII are UTF-8 percent-encoded.
    Reserved characters keep whatever form they had, because ``/a%2Fb`` and
    ``/a/b`` are different resources.
    """
    out: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if char == "%" and i + 2 < length and text[i + 1] in _HEX and text[i + 2] in _HEX:
            decoded = chr(int(text[i + 1 : i + 3], 16))
            out.append(decoded if decoded in _UNRESERVED else "%" + text[i + 1 : i + 3].upper())
            i += 3
            continue
        if ord(char) > 0x7E or ord(char) <= 0x20:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8", "surrogatepass"))
        else:
            out.append(char)
        i += 1
    return "".join(out)


def product_token(user_agent: str) -> str:
    """The product token of a user agent string, lower-cased.

    ``"internal-link-graph/0.1 (+https://...)"`` becomes ``"internal-link-graph"``.
    """
    token = user_agent.strip().split(None, 1)[0] if user_agent.strip() else ""
    return token.split("/", 1)[0].lower()


def url_path(url: str) -> str:
    """The part of ``url`` that robots.txt rules are matched against."""
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return _encode_path(path)


@dataclass(frozen=True)
class RobotsRule:
    """One ``Allow`` or ``Disallow`` line."""

    allow: bool
    pattern: str
    line: int = 0

    @property
    def directive(self) -> str:
        return "Allow" if self.allow else "Disallow"

    @property
    def specificity(self) -> int:
        """Length used for precedence (the longest matching rule wins)."""
        return len(self.pattern)

    def matches(self, path: str) -> bool:
        """Whether this rule's pattern matches the already encoded ``path``.

        Wildcards are matched greedily left to right, which is exact for
        patterns that only contain ``*`` and runs in linear time, so a hostile
        robots.txt cannot cause catastrophic backtracking.
        """
        pattern = self.pattern
        anchored = pattern.endswith("$")
        if anchored:
            pattern = pattern[:-1]
        segments = pattern.split("*")
        first = segments[0]
        if not path.startswith(first):
            return False
        if len(segments) == 1:
            return not anchored or len(path) == len(first)
        position = len(first)
        for segment in segments[1:-1]:
            if not segment:
                continue
            found = path.find(segment, position)
            if found < 0:
                return False
            position = found + len(segment)
        last = segments[-1]
        if anchored:
            return len(path) - len(last) >= position and path.endswith(last)
        return not last or path.find(last, position) >= 0

    def __str__(self) -> str:
        prefix = f"line {self.line}: " if self.line else ""
        return f"{prefix}{self.directive}: {self.pattern}"


@dataclass
class RobotsGroup:
    """A group of rules that applies to one or more user agents."""

    agents: list[str] = field(default_factory=list)
    rules: list[RobotsRule] = field(default_factory=list)
    crawl_delay: float | None = None


@dataclass(frozen=True)
class RobotsVerdict:
    """The outcome of checking one URL, with the rule that decided it."""

    url: str
    allowed: bool
    rule: RobotsRule | None = None
    agents: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "allowed": self.allowed,
            "rule": None
            if self.rule is None
            else {
                "directive": self.rule.directive,
                "pattern": self.rule.pattern,
                "line": self.rule.line,
            },
            "group": list(self.agents),
            "reason": self.reason,
        }


@dataclass
class RobotsRules:
    """Parsed robots.txt of one origin."""

    groups: list[RobotsGroup] = field(default_factory=list)
    allow_all: bool = False
    disallow_all: bool = False
    sitemaps: list[str] = field(default_factory=list)
    status: int = 0
    truncated: bool = False

    def _select(self, user_agent: str) -> tuple[list[RobotsRule], float | None, tuple[str, ...]]:
        """Merge the groups that apply to ``user_agent``."""
        token = product_token(user_agent)
        chosen = [group for group in self.groups if token and token in group.agents]
        agents: tuple[str, ...] = (token,) if chosen else ()
        if not chosen:
            chosen = [group for group in self.groups if "*" in group.agents]
            agents = ("*",) if chosen else ()
        rules = [rule for group in chosen for rule in group.rules]
        delay = next((g.crawl_delay for g in chosen if g.crawl_delay is not None), None)
        return rules, delay, agents

    def check(self, user_agent: str, url: str) -> RobotsVerdict:
        """Decide whether ``user_agent`` may fetch ``url`` and explain why."""
        if self.disallow_all:
            return RobotsVerdict(url, False, reason=f"robots.txt status {self.status}")
        if self.allow_all:
            return RobotsVerdict(url, True, reason=f"robots.txt status {self.status}")
        path = url_path(url)
        if path == "/robots.txt":
            return RobotsVerdict(url, True, reason="robots.txt is always allowed")
        rules, _delay, agents = self._select(user_agent)
        if not agents:
            return RobotsVerdict(url, True, reason="no group for this user agent")
        best: RobotsRule | None = None
        for rule in rules:
            if not rule.matches(path):
                continue
            if (
                best is None
                or rule.specificity > best.specificity
                or (rule.specificity == best.specificity and rule.allow and not best.allow)
            ):
                best = rule
        if best is None:
            return RobotsVerdict(url, True, agents=agents, reason="no matching rule")
        return RobotsVerdict(url, best.allow, rule=best, agents=agents)

    def can_fetch(self, user_agent: str, url: str) -> bool:
        return self.check(user_agent, url).allowed

    def crawl_delay(self, user_agent: str) -> float | None:
        if self.allow_all or self.disallow_all:
            return None
        return self._select(user_agent)[1]


def _parse_delay(value: str) -> float | None:
    try:
        delay = float(value)
    except ValueError:
        return None
    if delay != delay or delay < 0 or delay == float("inf"):  # NaN, negative, infinite
        return None
    return delay


def parse_robots(text: str, status: int = 200) -> RobotsRules:
    """Build :class:`RobotsRules` from the text of a robots.txt file."""
    truncated = len(text.encode("utf-8", "surrogatepass")) > MAX_ROBOTS_BYTES
    if truncated:
        text = text.encode("utf-8", "surrogatepass")[:MAX_ROBOTS_BYTES].decode("utf-8", "ignore")
    text = text.lstrip("﻿")
    groups: list[RobotsGroup] = []
    sitemaps: list[str] = []
    current: RobotsGroup | None = None
    collecting_agents = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key in _AGENT_KEYS:
            if not collecting_agents or current is None:
                current = RobotsGroup()
                groups.append(current)
                collecting_agents = True
            agent = "*" if value.startswith("*") else product_token(value)
            if agent and agent not in current.agents:
                current.agents.append(agent)
            continue
        if key == "sitemap":
            # Sitemap lines are global and do not end the current group.
            if value and value not in sitemaps:
                sitemaps.append(value)
            continue
        if current is None:
            continue  # rules before the first user-agent line apply to nobody
        if key in _RULE_KEYS:
            collecting_agents = False
            if not value:
                continue  # an empty Disallow (or Allow) matches nothing
            if not value.startswith(("/", "*")):
                value = "/" + value
            current.rules.append(RobotsRule(_RULE_KEYS[key], _encode_path(value), number))
        elif key == "crawl-delay":
            collecting_agents = False
            delay = _parse_delay(value)
            if delay is not None and current.crawl_delay is None:
                current.crawl_delay = delay
    return RobotsRules(groups=groups, sitemaps=sitemaps, status=status, truncated=truncated)


def rules_for_response(status: int, body: bytes, content_type: str = "") -> RobotsRules:
    """Translate an HTTP response for ``/robots.txt`` into rules.

    Following the conventions used by the major search engines:

    * 2xx - parse the file,
    * 401 / 403 - the whole site is off limits,
    * other 4xx - no restrictions,
    * 5xx or network errors - be conservative and do not crawl.
    """
    if 200 <= status < 300:
        truncated = len(body) > MAX_ROBOTS_BYTES
        rules = parse_robots(decode_body(body[:MAX_ROBOTS_BYTES], content_type), status=status)
        rules.truncated = rules.truncated or truncated
        return rules
    if status in (401, 403):
        return RobotsRules(disallow_all=True, status=status)
    if 400 <= status < 500:
        return RobotsRules(allow_all=True, status=status)
    return RobotsRules(disallow_all=True, status=status)


def fetch_robots(fetcher: Fetcher, url: str) -> RobotsRules:
    """Fetch and parse the robots.txt that governs ``url``."""
    origin = origin_of(url)
    try:
        response, _ = fetch_following(fetcher, origin + "/robots.txt")
        return rules_for_response(response.status, response.body, response.header("content-type"))
    except Exception:  # a broken fetcher must not take the crawl down
        return RobotsRules(disallow_all=True)


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
            rules = fetch_robots(self.fetcher, url)
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
