"""XML sitemap parsing (urlset, sitemap index, gzip and plain-text variants)."""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from xml.etree import ElementTree

from .fetcher import Fetcher, fetch_following
from .html_extract import decode_body

GZIP_MAGIC = b"\x1f\x8b"
MAX_DECOMPRESSED_BYTES = 60_000_000


class SitemapError(ValueError):
    """Raised when a sitemap document cannot be understood."""


@dataclass
class SitemapDocument:
    """Parsed sitemap: ``kind`` is ``"urlset"``, ``"index"`` or ``"text"``."""

    kind: str
    urls: list[str] = field(default_factory=list)


@dataclass
class SitemapResult:
    """Outcome of walking one or more sitemaps."""

    urls: list[str] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def maybe_gunzip(data: bytes) -> bytes:
    """Transparently decompress gzip data (``.xml.gz`` sitemaps)."""
    if not data.startswith(GZIP_MAGIC):
        return data
    try:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        result = decompressor.decompress(data, MAX_DECOMPRESSED_BYTES)
    except zlib.error as exc:
        raise SitemapError(f"invalid gzip data: {exc}") from exc
    if decompressor.unconsumed_tail:
        raise SitemapError("decompressed sitemap is too large")
    return result


def parse_sitemap(data: bytes) -> SitemapDocument:
    """Parse sitemap bytes into a :class:`SitemapDocument`.

    Documents with a DTD are rejected to stay clear of entity expansion tricks.
    """
    data = maybe_gunzip(data)
    stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not stripped:
        raise SitemapError("empty sitemap")
    if not stripped.startswith(b"<"):
        text = decode_body(stripped)
        urls = [
            line.strip()
            for line in text.splitlines()
            if line.strip().lower().startswith(("http://", "https://"))
        ]
        if not urls:
            raise SitemapError("no URLs found in plain-text sitemap")
        return SitemapDocument(kind="text", urls=urls)
    head = stripped[:2048].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise SitemapError("sitemaps with a DTD are not supported")
    try:
        root = ElementTree.fromstring(stripped)
    except ElementTree.ParseError as exc:
        raise SitemapError(f"invalid XML: {exc}") from exc
    root_name = _local(root.tag)
    if root_name == "sitemapindex":
        kind, entry_name = "index", "sitemap"
    elif root_name == "urlset":
        kind, entry_name = "urlset", "url"
    else:
        raise SitemapError(f"unexpected root element <{root_name}>")
    urls: list[str] = []
    for entry in root:
        if _local(entry.tag) != entry_name:
            continue
        for child in entry:
            if _local(child.tag) == "loc" and child.text and child.text.strip():
                urls.append(child.text.strip())
                break
    return SitemapDocument(kind=kind, urls=urls)


def collect_sitemap_urls(
    fetcher: Fetcher,
    sitemap_urls: list[str],
    max_sitemaps: int = 50,
    max_urls: int = 50_000,
) -> SitemapResult:
    """Fetch sitemaps (following sitemap indexes) and gather page URLs.

    Order is preserved and duplicates are dropped.  Nested indexes are walked
    breadth-first with loop protection; ``max_sitemaps`` bounds the number of
    files fetched and ``max_urls`` the number of page URLs returned.
    """
    result = SitemapResult()
    queue = list(dict.fromkeys(sitemap_urls))
    visited: set[str] = set()
    seen_urls: set[str] = set()
    while queue and len(result.fetched) < max_sitemaps:
        sitemap_url = queue.pop(0)
        if sitemap_url in visited:
            continue
        visited.add(sitemap_url)
        try:
            response, _ = fetch_following(fetcher, sitemap_url)
        except Exception as exc:
            result.errors[sitemap_url] = f"fetch failed: {exc}"
            continue
        if response.status != 200:
            result.errors[sitemap_url] = response.error or f"HTTP {response.status}"
            continue
        try:
            document = parse_sitemap(response.body)
        except SitemapError as exc:
            result.errors[sitemap_url] = str(exc)
            continue
        result.fetched.append(sitemap_url)
        if document.kind == "index":
            queue.extend(url for url in document.urls if url not in visited)
            continue
        for url in document.urls:
            if url in seen_urls:
                continue
            if len(result.urls) >= max_urls:
                return result
            seen_urls.add(url)
            result.urls.append(url)
    return result
