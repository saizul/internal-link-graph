"""Build a :class:`SiteData` from files instead of crawling.

Supported inputs:

* this tool's own crawl JSON (``internal-link-graph crawl -o crawl.json``),
* a Screaming Frog "All Inlinks" style CSV export,
* a plain edge list as CSV (``source,target[,anchor,...]``) or JSON.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .models import NOFOLLOW_RELS, Link, SiteData
from .urlnorm import DEFAULT_OPTIONS, NormalizeOptions, is_internal, normalize_url

FORMATS = ("auto", "crawl", "edges", "screaming-frog")

_POSITION_ALIASES = {
    "navigation": "nav",
    "nav": "nav",
    "header": "header",
    "footer": "footer",
    "content": "main",
    "main": "main",
    "aside": "aside",
    "sidebar": "aside",
    "body": "body",
}
_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "nofollow"})
_SF_LINK_TYPES = frozenset({"hyperlink", "ahref", "a", ""})

_SOURCE_COLUMNS = ("source", "from", "source url", "source_url", "src")
_TARGET_COLUMNS = ("target", "destination", "to", "target url", "target_url", "dest", "dst")
_ANCHOR_COLUMNS = ("anchor", "anchor text", "anchor_text", "text")


class ImportFormatError(ValueError):
    """Raised when an input file cannot be interpreted."""


def _read_text(path: str | Path) -> str:
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _lower_keys(row: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(key).strip().lower(): ("" if value is None else str(value).strip())
        for key, value in row.items()
        if key is not None
    }


def _first(row: Mapping[str, str], names: Iterable[str]) -> str:
    for name in names:
        value = row.get(name)
        if value:
            return value
    return ""


def _position(value: str) -> str:
    return _POSITION_ALIASES.get(value.strip().lower(), "body")


def _parse_rel(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.lower().replace(",", " ").split() if part)


def _to_int(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _finish(site: SiteData, start_url: str | None, options: NormalizeOptions) -> SiteData:
    site.ensure_link_targets()
    if start_url:
        normalised = normalize_url(start_url, options=options)
        if normalised is None:
            raise ImportFormatError(f"invalid start URL {start_url!r}")
        site.start_url = normalised
        site.page(normalised)
    if not site.start_url:
        site.start_url = guess_start_url(site)
    if site.start_url:
        for link in site.links:
            link.internal = is_internal(link.target, site.start_url, include_subdomains=False)
        for url in [u for u in site.pages if not is_internal(u, site.start_url)]:
            # External link targets are kept as links but are not pages of this site.
            if not any(link.source == url for link in site.links):
                del site.pages[url]
    return site


def guess_start_url(site: SiteData) -> str:
    """Best guess for the homepage: the shortest URL among link sources."""
    candidates = {link.source for link in site.links} or set(site.pages)
    if not candidates:
        return ""
    roots = [url for url in candidates if url.rstrip("/").count("/") == 2 and "?" not in url]
    pool = roots or list(candidates)
    return min(pool, key=lambda url: (len(url), url))


# --------------------------------------------------------------------------
# Edge lists
# --------------------------------------------------------------------------


def _edge_from_mapping(row: Mapping[str, Any], options: NormalizeOptions) -> Link | None:
    data = _lower_keys(row)
    source = normalize_url(_first(data, _SOURCE_COLUMNS), options=options)
    target = normalize_url(_first(data, _TARGET_COLUMNS), options=options)
    if source is None or target is None:
        return None
    rel = _parse_rel(data.get("rel", ""))
    nofollow = data.get("nofollow", "").lower() in _TRUE_VALUES
    if data.get("follow", "").lower() in ("false", "0", "no"):
        nofollow = True
    nofollow = nofollow or any(value in NOFOLLOW_RELS for value in rel)
    return Link(
        source=source,
        target=target,
        anchor=_first(data, _ANCHOR_COLUMNS),
        rel=rel,
        position=_position(data.get("position", "") or data.get("link position", "")),
        is_image=data.get("is_image", "").lower() in _TRUE_VALUES,
        nofollow=nofollow,
    )


def read_edge_csv(
    text: str,
    start_url: str | None = None,
    options: NormalizeOptions = DEFAULT_OPTIONS,
) -> SiteData:
    """Parse an edge-list CSV.

    With a header row the columns ``source`` and ``target`` are required and
    ``anchor``, ``nofollow``, ``rel`` and ``position`` are optional.  Without a
    header the first two columns are source and target and the third (if
    present) the anchor text.  Lines starting with ``#`` are ignored.
    """
    lines = [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        raise ImportFormatError("edge list is empty")
    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(lines, dialect))
    header = [cell.strip().lower() for cell in rows[0]]
    site = SiteData(meta={"source": "edges"})
    has_header = any(name in header for name in _SOURCE_COLUMNS) and any(
        name in header for name in _TARGET_COLUMNS
    )
    if has_header:
        records: Iterable[Mapping[str, Any]] = (
            dict(zip(header, row, strict=False)) for row in rows[1:]
        )
    else:
        if len(rows[0]) < 2:
            raise ImportFormatError("edge list needs at least two columns (source, target)")
        names = ["source", "target", "anchor", "nofollow"]
        records = (dict(zip(names, row, strict=False)) for row in rows)
    skipped = 0
    for record in records:
        link = _edge_from_mapping(record, options)
        if link is None:
            skipped += 1
            continue
        site.links.append(link)
    if not site.links:
        raise ImportFormatError("no valid edges found (need absolute http(s) URLs)")
    site.meta["skipped_rows"] = skipped
    return _finish(site, start_url, options)


def read_edge_json(
    payload: Any,
    start_url: str | None = None,
    options: NormalizeOptions = DEFAULT_OPTIONS,
) -> SiteData:
    """Parse an edge list given as decoded JSON.

    Accepted shapes::

        [["https://a/", "https://a/b"], ...]
        [{"source": "...", "target": "...", "anchor": "..."}, ...]
        {"start_url": "...", "edges": [...], "nodes": [{"url": "...", "title": "..."}],
         "sitemap": ["..."]}
    """
    site = SiteData(meta={"source": "edges"})
    nodes: list[Any] = []
    sitemap: list[Any] = []
    if isinstance(payload, dict):
        edges = payload.get("edges") or payload.get("links") or []
        nodes = payload.get("nodes") or payload.get("pages") or []
        sitemap = payload.get("sitemap") or payload.get("sitemap_urls") or []
        start_url = start_url or payload.get("start_url") or None
    elif isinstance(payload, list):
        edges = payload
    else:
        raise ImportFormatError("edge list JSON must be a list or an object with 'edges'")
    skipped = 0
    for edge in edges:
        if isinstance(edge, (list, tuple)) and len(edge) >= 2:
            record: Mapping[str, Any] = {
                "source": edge[0],
                "target": edge[1],
                "anchor": edge[2] if len(edge) > 2 else "",
            }
        elif isinstance(edge, dict):
            record = dict(edge)
            if isinstance(record.get("rel"), list):
                record["rel"] = " ".join(str(value) for value in record["rel"])
        else:
            skipped += 1
            continue
        link = _edge_from_mapping(record, options)
        if link is None:
            skipped += 1
            continue
        site.links.append(link)
    for node in nodes:
        if isinstance(node, str):
            node = {"url": node}
        if not isinstance(node, dict) or "url" not in node:
            continue
        url = normalize_url(str(node["url"]), options=options)
        if url is None:
            continue
        page = site.page(url)
        page.title = str(node.get("title", "") or "")
        page.h1 = str(node.get("h1", "") or "")
        page.text = str(node.get("text", "") or "")
        page.status = _to_int(str(node.get("status", 0) or 0))
        page.word_count = _to_int(str(node.get("word_count", 0) or 0))
        page.canonical = str(node.get("canonical", "") or "")
        page.meta_robots = str(node.get("meta_robots", "") or "")
        page.redirect_target = str(node.get("redirect_target", "") or "")
    for raw in sitemap:
        url = normalize_url(str(raw), options=options)
        if url is not None:
            site.sitemap_urls.add(url)
    if not site.links and not site.pages:
        raise ImportFormatError("no valid edges or nodes found")
    site.meta["skipped_rows"] = skipped
    return _finish(site, start_url, options)


# --------------------------------------------------------------------------
# Screaming Frog
# --------------------------------------------------------------------------


def looks_like_screaming_frog(header: Iterable[str]) -> bool:
    names = {name.strip().lower() for name in header}
    return {"source", "destination"} <= names and bool(names & {"type", "status code", "follow"})


def read_screaming_frog_csv(
    text: str,
    start_url: str | None = None,
    options: NormalizeOptions = DEFAULT_OPTIONS,
) -> SiteData:
    """Parse a Screaming Frog "All Inlinks" (bulk export) CSV.

    Relevant columns: ``Type``, ``Source``, ``Destination``, ``Anchor``,
    ``Alt Text``, ``Follow``, ``Rel``, ``Status Code``, ``Link Position``.
    Only hyperlinks are imported; image, CSS, JavaScript and canonical rows
    are ignored.  The destination status code is stored on the target page so
    broken links and redirects can be reported.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not looks_like_screaming_frog(reader.fieldnames):
        raise ImportFormatError(
            "not a Screaming Frog inlinks export (need Source and Destination columns)"
        )
    site = SiteData(meta={"source": "screaming-frog"})
    skipped = 0
    for raw in reader:
        row = _lower_keys(raw)
        if row.get("type", "").lower() not in _SF_LINK_TYPES:
            skipped += 1
            continue
        source = normalize_url(row.get("source", ""), options=options)
        target = normalize_url(row.get("destination", ""), options=options)
        if source is None or target is None:
            skipped += 1
            continue
        rel = _parse_rel(row.get("rel", ""))
        follow = row.get("follow", "true").lower()
        nofollow = follow in ("false", "0", "no") or any(value in NOFOLLOW_RELS for value in rel)
        anchor = row.get("anchor", "") or row.get("anchor text", "")
        alt = row.get("alt text", "")
        site.links.append(
            Link(
                source=source,
                target=target,
                anchor=anchor or alt,
                rel=rel,
                position=_position(row.get("link position", "")),
                is_image=bool(alt) and not anchor,
                nofollow=nofollow,
            )
        )
        status = _to_int(row.get("status code", ""))
        site.page(source).status = site.page(source).status or 200
        site.page(source).crawled = True
        if status:
            target_page = site.page(target)
            target_page.status = status
            target_page.crawled = True
    if not site.links:
        raise ImportFormatError("no hyperlink rows found in the export")
    site.meta["skipped_rows"] = skipped
    site.meta["source"] = "screaming-frog"
    return _finish(site, start_url, options)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def detect_format(path: str | Path, text: str | None = None) -> str:
    """Guess the format of an input file from its name and content."""
    if text is None:
        text = _read_text(path)
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ImportFormatError(f"{path}: invalid JSON ({exc})") from exc
        if isinstance(payload, dict) and str(payload.get("format", "")).endswith("/crawl"):
            return "crawl"
        return "edges"
    first_line = stripped.splitlines()[0] if stripped else ""
    try:
        header = next(csv.reader([first_line]))
    except (csv.Error, StopIteration):
        header = []
    if looks_like_screaming_frog(header):
        return "screaming-frog"
    return "edges"


def load_site(
    path: str | Path,
    fmt: str = "auto",
    start_url: str | None = None,
    options: NormalizeOptions = DEFAULT_OPTIONS,
) -> SiteData:
    """Load a crawl file, edge list or Screaming Frog export from ``path``."""
    if fmt not in FORMATS:
        raise ImportFormatError(f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}")
    file_path = Path(path)
    if not file_path.is_file():
        raise ImportFormatError(f"{path}: no such file")
    text = _read_text(file_path)
    if fmt == "auto":
        fmt = detect_format(file_path, text)
    if fmt == "crawl":
        try:
            site = SiteData.from_dict(json.loads(text))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ImportFormatError(f"{path}: not a valid crawl file ({exc})") from exc
        if start_url:
            normalised = normalize_url(start_url, options=options)
            if normalised is None:
                raise ImportFormatError(f"invalid start URL {start_url!r}")
            site.start_url = normalised
        return site
    if fmt == "screaming-frog":
        return read_screaming_frog_csv(text, start_url, options)
    if text.lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ImportFormatError(f"{path}: invalid JSON ({exc})") from exc
        return read_edge_json(payload, start_url, options)
    return read_edge_csv(text, start_url, options)
