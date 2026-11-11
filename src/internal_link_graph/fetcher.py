"""HTTP fetching abstraction.

The crawler only depends on the tiny :class:`Fetcher` protocol, which makes it
trivial to swap the real network implementation (:class:`UrllibFetcher`) for
an in-memory site (:class:`DictFetcher`) in tests and examples.

Fetchers never follow redirects themselves; the crawler does that so it can
record every hop of a redirect chain.
"""

from __future__ import annotations

import gzip
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin

from . import __version__
from .models import REDIRECT_STATUSES

DEFAULT_USER_AGENT = (
    f"internal-link-graph/{__version__} (+https://github.com/saizul/internal-link-graph)"
)
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


@dataclass
class Response:
    """Result of fetching one URL. ``status`` is ``0`` when the request failed."""

    url: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    error: str = ""
    elapsed: float = 0.0

    def __post_init__(self) -> None:
        self.headers = {str(key).lower(): str(value) for key, value in self.headers.items()}
        if isinstance(self.body, str):
            self.body = self.body.encode("utf-8")

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    @property
    def content_type(self) -> str:
        return self.header("content-type").split(";", 1)[0].strip().lower()

    @property
    def is_html(self) -> bool:
        content_type = self.content_type
        if not content_type:
            # Servers that omit the header almost always serve HTML; sniff to be sure.
            head = self.body[:512].lstrip().lower()
            return head.startswith((b"<!doctype html", b"<html")) or b"<head" in head
        return content_type in HTML_CONTENT_TYPES

    @property
    def is_redirect(self) -> bool:
        return self.status in REDIRECT_STATUSES

    @property
    def location(self) -> str:
        return self.header("location").strip()

    @property
    def ok(self) -> bool:
        return self.status == 200


class Fetcher(Protocol):
    """Anything with a ``fetch(url) -> Response`` method."""

    def fetch(self, url: str) -> Response: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _decompress(body: bytes, encoding: str) -> bytes:
    encoding = encoding.lower().strip()
    try:
        if encoding in ("gzip", "x-gzip"):
            return gzip.decompress(body)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, EOFError, zlib.error):
        return body
    return body


class UrllibFetcher:
    """Real HTTP fetcher built on :mod:`urllib.request`.

    ``opener`` can be injected for testing; it only needs an
    ``open(request, timeout=...)`` method behaving like urllib's opener.
    """

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 15.0,
        max_bytes: int = 5_000_000,
        extra_headers: Mapping[str, str] | None = None,
        opener: Any = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.extra_headers = dict(extra_headers or {})
        self._opener = opener or urllib.request.build_opener(_NoRedirectHandler())

    def _request(self, url: str) -> urllib.request.Request:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
            "Accept-Encoding": "gzip, deflate",
        }
        headers.update(self.extra_headers)
        return urllib.request.Request(url, headers=headers, method="GET")

    def fetch(self, url: str) -> Response:
        started = time.monotonic()
        try:
            request = self._request(url)
        except ValueError as exc:
            return Response(url=url, status=0, error=f"invalid URL: {exc}")
        try:
            with self._opener.open(request, timeout=self.timeout) as handle:
                return self._build(url, handle.status, handle.headers, handle, started)
        except urllib.error.HTTPError as exc:
            try:
                return self._build(url, exc.code, exc.headers, exc, started)
            finally:
                exc.close()
        except urllib.error.URLError as exc:
            return self._failure(url, f"connection error: {exc.reason}", started)
        except TimeoutError:
            return self._failure(url, "timeout", started)
        except (OSError, ValueError) as exc:
            return self._failure(url, f"{type(exc).__name__}: {exc}", started)

    def _build(
        self, url: str, status: int, raw_headers: Any, handle: Any, started: float
    ) -> Response:
        pairs = raw_headers.items() if raw_headers else []
        headers = {key.lower(): value for key, value in pairs}
        try:
            body = handle.read(self.max_bytes + 1)
        except (OSError, ValueError):
            body = b""
        truncated = len(body) > self.max_bytes
        body = body[: self.max_bytes]
        encoding = headers.get("content-encoding", "")
        if encoding and not truncated:
            body = _decompress(body, encoding)
        return Response(
            url=url,
            status=int(status),
            headers=headers,
            body=body,
            elapsed=time.monotonic() - started,
        )

    @staticmethod
    def _failure(url: str, message: str, started: float) -> Response:
        return Response(url=url, status=0, error=message, elapsed=time.monotonic() - started)


SiteEntry = Response | tuple | str | bytes | int | Callable[[str], Response]


class DictFetcher:
    """In-memory fake site for tests, demos and dry runs.

    ``site`` maps URLs to one of:

    * ``str`` / ``bytes`` - an HTML body served with status 200,
    * ``int`` - a bare status code (for example ``404``),
    * ``(status, headers, body)`` or ``(status, body)`` tuples,
    * a ready :class:`Response`,
    * a callable taking the URL and returning a :class:`Response`.

    Unknown URLs answer 404.  Every request is appended to :attr:`requests`.
    """

    def __init__(self, site: Mapping[str, SiteEntry] | None = None) -> None:
        self.site: dict[str, SiteEntry] = dict(site or {})
        self.requests: list[str] = []
        self._lock = threading.Lock()

    def add(self, url: str, entry: SiteEntry) -> None:
        self.site[url] = entry

    def redirect(self, url: str, target: str, status: int = 301) -> None:
        self.site[url] = (status, {"Location": target}, b"")

    def count(self, url: str) -> int:
        return self.requests.count(url)

    def fetch(self, url: str) -> Response:
        with self._lock:
            self.requests.append(url)
        entry = self.site.get(url)
        if entry is None:
            return Response(url=url, status=404, headers={"content-type": "text/html"})
        if callable(entry):
            return entry(url)
        if isinstance(entry, Response):
            return Response(
                url=url,
                status=entry.status,
                headers=dict(entry.headers),
                body=entry.body,
                error=entry.error,
            )
        if isinstance(entry, int):
            return Response(url=url, status=entry, headers={"content-type": "text/html"})
        if isinstance(entry, (str, bytes)):
            return Response(
                url=url,
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=entry.encode("utf-8") if isinstance(entry, str) else entry,
            )
        if isinstance(entry, tuple):
            if len(entry) == 3:
                status, headers, body = entry
            elif len(entry) == 2:
                status, body = entry
                headers = {}
            else:
                raise ValueError(f"unsupported site entry for {url!r}: {entry!r}")
            headers = dict(headers)
            if not any(key.lower() == "content-type" for key in headers):
                headers["content-type"] = "text/html; charset=utf-8"
            if isinstance(body, str):
                body = body.encode("utf-8")
            return Response(url=url, status=int(status), headers=headers, body=body)
        raise ValueError(f"unsupported site entry for {url!r}: {entry!r}")


def fetch_following(
    fetcher: Fetcher, url: str, max_redirects: int = 5
) -> tuple[Response, list[str]]:
    """Fetch ``url`` following redirects; returns the last response and the hop list.

    Used for auxiliary resources (robots.txt, sitemaps) where the chain itself
    is not interesting to the analysis.
    """
    hops = [url]
    current = url
    response = fetcher.fetch(current)
    for _ in range(max_redirects):
        if not (response.is_redirect and response.location):
            break
        try:
            nxt = urljoin(current, response.location)
        except ValueError:
            break
        if nxt in hops:
            break
        hops.append(nxt)
        current = nxt
        response = fetcher.fetch(current)
    return response, hops
