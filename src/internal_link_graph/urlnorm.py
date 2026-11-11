"""URL normalisation and classification helpers.

Two URLs that a browser (and a search engine) would treat as the same document
should map to the same string, otherwise the link graph ends up with duplicate
nodes and diluted metrics.  The rules implemented here follow RFC 3986 section 6
plus a handful of pragmatic, SEO-specific conventions (tracking parameter
removal, trailing slash policy).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}

#: Query parameters that never change the document and only exist for analytics.
TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "gclsrc",
        "dclid",
        "gbraid",
        "wbraid",
        "fbclid",
        "msclkid",
        "yclid",
        "twclid",
        "ttclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "_hsenc",
        "_hsmi",
        "hsctatracking",
        "mkt_tok",
        "oly_anon_id",
        "oly_enc_id",
        "vero_id",
        "vero_conv",
        "srsltid",
        "ref_src",
        "s_cid",
    }
)

#: Prefixes of analytics parameters (``utm_source``, ``pk_campaign`` ...).
TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_", "stm_")

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = frozenset("0123456789abcdefABCDEF")
_PATH_SAFE = frozenset("/!$&'()*+,;=:@")
_QUERY_SAFE = frozenset("!$'()*+,;:@/?")
_INDEX_RE = re.compile(r"^(index|default)\.(html?|php|aspx?|jsp|cfm)$", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_MULTI_SLASH_RE = re.compile(r"/{2,}")

TRAILING_SLASH_POLICIES = ("keep", "add", "strip")


@dataclass(frozen=True)
class NormalizeOptions:
    """Knobs controlling :func:`normalize_url`.

    ``trailing_slash`` is one of ``"keep"`` (default, leave paths alone),
    ``"add"`` (append a slash to extension-less paths) or ``"strip"`` (remove
    it everywhere except the root).
    """

    trailing_slash: str = "keep"
    strip_tracking: bool = True
    extra_tracking_params: frozenset[str] = frozenset()
    sort_query: bool = True
    strip_query: bool = False
    strip_www: bool = False
    collapse_slashes: bool = True
    strip_index: bool = False
    force_https: bool = False

    def __post_init__(self) -> None:
        if self.trailing_slash not in TRAILING_SLASH_POLICIES:
            raise ValueError(
                f"trailing_slash must be one of {TRAILING_SLASH_POLICIES}, "
                f"got {self.trailing_slash!r}"
            )


DEFAULT_OPTIONS = NormalizeOptions()


def normalize_percent_encoding(text: str, safe: frozenset[str] = _PATH_SAFE) -> str:
    """Normalise percent-encoding in a URL component.

    * escapes of unreserved characters are decoded (``%7E`` -> ``~``),
    * remaining escapes get upper-case hex digits (``%2f`` -> ``%2F``),
    * characters that may not appear literally are UTF-8 percent-encoded,
    * a stray ``%`` that does not start a valid escape becomes ``%25``.
    """
    out: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch == "%":
            pair = text[i + 1 : i + 3]
            if len(pair) == 2 and pair[0] in _HEX and pair[1] in _HEX:
                decoded = chr(int(pair, 16))
                out.append(decoded if decoded in _UNRESERVED else "%" + pair.upper())
                i += 3
                continue
            out.append("%25")
            i += 1
            continue
        if ch in _UNRESERVED or ch in safe:
            out.append(ch)
        else:
            out.extend(f"%{byte:02X}" for byte in ch.encode("utf-8", "surrogatepass"))
        i += 1
    return "".join(out)


def remove_dot_segments(path: str) -> str:
    """Resolve ``.`` and ``..`` segments as described in RFC 3986 section 5.2.4."""
    if not path:
        return path
    output: list[str] = []
    for segment in path.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if len(output) > 1 or (output and output[0] != ""):
                output.pop()
            continue
        output.append(segment)
    last = path.rsplit("/", 1)[-1]
    if last in (".", ".."):
        output.append("")
    result = "/".join(output)
    if path.startswith("/") and not result.startswith("/"):
        result = "/" + result
    return result


def is_tracking_param(name: str, extra: frozenset[str] = frozenset()) -> bool:
    """Return True if a query parameter name is a known analytics parameter."""
    lowered = name.lower()
    if lowered in TRACKING_PARAMS or lowered in extra:
        return True
    return lowered.startswith(TRACKING_PREFIXES)


def _normalize_query(query: str, options: NormalizeOptions) -> str:
    if not query or options.strip_query:
        return ""
    pairs: list[tuple[str, str | None]] = []
    for chunk in query.split("&"):
        if not chunk:
            continue
        if "=" in chunk:
            key, value = chunk.split("=", 1)
        else:
            key, value = chunk, None
        key = normalize_percent_encoding(key, _QUERY_SAFE)
        if options.strip_tracking and is_tracking_param(key, options.extra_tracking_params):
            continue
        if value is not None:
            value = normalize_percent_encoding(value, _QUERY_SAFE | {"="})
        pairs.append((key, value))
    if options.sort_query:
        pairs.sort(key=lambda kv: (kv[0], kv[1] or ""))
    return "&".join(k if v is None else f"{k}={v}" for k, v in pairs)


def _normalize_host(host: str, options: NormalizeOptions) -> str:
    host = host.lower().rstrip(".")
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            pass
    if options.strip_www and host.startswith("www.") and host.count(".") >= 2:
        host = host[4:]
    return host


def _normalize_path(path: str, options: NormalizeOptions) -> str:
    if not path:
        return "/"
    path = remove_dot_segments(path)
    if options.collapse_slashes:
        path = _MULTI_SLASH_RE.sub("/", path)
    path = normalize_percent_encoding(path, _PATH_SAFE)
    if not path.startswith("/"):
        path = "/" + path
    if options.strip_index:
        head, _, tail = path.rpartition("/")
        if _INDEX_RE.match(tail):
            path = head + "/"
    if options.trailing_slash == "add":
        last = path.rsplit("/", 1)[-1]
        if last and "." not in last:
            path += "/"
    elif options.trailing_slash == "strip" and len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return path


def normalize_url(
    url: str | None,
    base: str | None = None,
    options: NormalizeOptions = DEFAULT_OPTIONS,
) -> str | None:
    """Return the canonical form of ``url`` or ``None`` if it is not crawlable.

    ``base`` is used to resolve relative references.  Non-HTTP schemes
    (``mailto:``, ``javascript:``, ``tel:``, ``data:`` ...) yield ``None``.
    """
    if url is None:
        return None
    url = url.strip()
    # Browsers drop tabs and newlines found inside a URL.
    url = url.replace("\t", "").replace("\n", "").replace("\r", "")
    if not url and base is None:
        return None
    scheme_match = _SCHEME_RE.match(url)
    if scheme_match and scheme_match.group(1).lower() not in DEFAULT_PORTS:
        return None
    if base is not None:
        try:
            url = urljoin(base, url)
        except ValueError:
            return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    if options.force_https:
        if scheme == "http" and port == 80:
            port = None
        scheme = "https"
    host = _normalize_host(parts.hostname, options)
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc = f"{host}:{port}"
    path = _normalize_path(parts.path, options)
    query = _normalize_query(parts.query, options)
    return urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    """Lower-cased host name of ``url`` (empty string when it cannot be parsed)."""
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` of ``url``."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _bare_host(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def same_site(host_a: str, host_b: str, include_subdomains: bool = False) -> bool:
    """Compare two hosts, treating ``www.`` as equivalent to the bare domain.

    With ``include_subdomains`` every subdomain of ``host_b`` counts as the
    same site (``blog.example.com`` vs ``example.com``).
    """
    a = _bare_host(host_a.lower().rstrip("."))
    b = _bare_host(host_b.lower().rstrip("."))
    if not a or not b:
        return False
    if a == b:
        return True
    return include_subdomains and a.endswith("." + b)


def is_internal(url: str, root_url: str, include_subdomains: bool = False) -> bool:
    """True when ``url`` belongs to the same site as ``root_url``."""
    return same_site(host_of(url), host_of(root_url), include_subdomains)


def path_segments(url: str) -> list[str]:
    """Non-empty path segments of ``url``."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return []
    return [segment for segment in path.split("/") if segment]


def url_directory(url: str, depth: int = 1) -> str:
    """Directory prefix of a URL used for topic clustering.

    ``https://example.com/blog/seo/post`` -> ``/blog/`` for ``depth=1`` and
    ``/blog/seo/`` for ``depth=2``.  Pages living directly under the root map
    to ``/``.  The final segment is treated as the page itself unless the URL
    ends with a slash.
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        return "/"
    segments = [segment for segment in path.split("/") if segment]
    directories = segments if path.endswith("/") and len(segments) > 1 else segments[:-1]
    if depth < 1 or not directories:
        return "/"
    return "/" + "/".join(directories[:depth]) + "/"


def url_extension(url: str) -> str:
    """Lower-cased file extension of the URL path without the dot (may be empty)."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    ext = posixpath.splitext(path)[1]
    return ext[1:].lower()


def short_path(url: str, max_length: int = 60) -> str:
    """Path + query of a URL, shortened in the middle for display purposes."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    text = parts.path or "/"
    if parts.query:
        text += "?" + parts.query
    if len(text) <= max_length:
        return text
    keep = max_length - 3
    head = keep // 2
    return text[:head] + "..." + text[-(keep - head) :]
