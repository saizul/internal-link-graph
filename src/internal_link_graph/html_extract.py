"""HTML parsing: links, anchors, positions and on-page signals.

Built on :mod:`html.parser` so that broken real-world markup never raises.
The parser keeps a stack of open elements which is used to decide where on
the page a link lives (navigation, header, footer, sidebar or main content).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

from .models import NOFOLLOW_RELS
from .textsim import count_words
from .urlnorm import DEFAULT_OPTIONS, NormalizeOptions, normalize_url

VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

#: Elements whose text content is never visible page copy.
SKIP_TEXT_ELEMENTS = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})

TAG_LANDMARKS = {
    "nav": "nav",
    "header": "header",
    "footer": "footer",
    "main": "main",
    "article": "main",
    "aside": "aside",
}

ROLE_LANDMARKS = {
    "navigation": "nav",
    "banner": "header",
    "contentinfo": "footer",
    "main": "main",
    "article": "main",
    "complementary": "aside",
}

#: ``id``/``class`` tokens of generic containers that hint at a page region.
CLASS_LANDMARKS = {
    "nav": "nav",
    "navbar": "nav",
    "navigation": "nav",
    "menu": "nav",
    "breadcrumb": "nav",
    "breadcrumbs": "nav",
    "header": "header",
    "site-header": "header",
    "masthead": "header",
    "footer": "footer",
    "site-footer": "footer",
    "sidebar": "aside",
    "widget-area": "aside",
    "content": "main",
    "main-content": "main",
    "entry-content": "main",
    "post-content": "main",
}

_CONTAINER_TAGS = frozenset({"div", "section", "ul", "ol", "table", "td"})
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "li",
        "ul",
        "ol",
        "br",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "footer",
        "nav",
        "aside",
        "main",
        "blockquote",
        "pre",
        "table",
        "figure",
        "figcaption",
        "form",
        "dd",
        "dt",
        "title",
    }
)
_WHITESPACE_RE = re.compile(r"\s+")
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE
)
_HEADER_CHARSET_RE = re.compile(r"charset\s*=\s*\"?([a-zA-Z0-9_\-]+)", re.IGNORECASE)


def collapse_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


@dataclass
class ExtractedLink:
    """A link as found in the markup, already resolved against the base URL."""

    url: str
    href: str
    anchor: str = ""
    rel: tuple[str, ...] = ()
    position: str = "body"
    is_image: bool = False

    @property
    def nofollow(self) -> bool:
        return any(value in NOFOLLOW_RELS for value in self.rel)


@dataclass
class PageExtract:
    """On-page data pulled out of one HTML document."""

    title: str = ""
    h1: str = ""
    h1_all: list[str] = field(default_factory=list)
    canonical: str = ""
    meta_robots: str = ""
    meta_description: str = ""
    lang: str = ""
    base_href: str = ""
    links: list[ExtractedLink] = field(default_factory=list)
    word_count: int = 0
    text: str = ""
    skipped_hrefs: list[str] = field(default_factory=list)


class _OpenAnchor:
    __slots__ = ("href", "rel", "position", "text", "alts", "fallback", "has_image")

    def __init__(self, href: str, rel: tuple[str, ...], position: str, fallback: str) -> None:
        self.href = href
        self.rel = rel
        self.position = position
        self.text: list[str] = []
        self.alts: list[str] = []
        self.fallback = fallback
        self.has_image = False


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str | None]] = []
        self.skip_depth = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.title_done = False
        self.h1_parts: list[str] | None = None
        self.h1_all: list[str] = []
        self.anchor: _OpenAnchor | None = None
        self.raw_links: list[_OpenAnchor] = []
        self.text_parts: list[str] = []
        self.canonical = ""
        self.robots: list[str] = []
        self.description = ""
        self.lang = ""
        self.base_href = ""

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _landmark(tag: str, attrs: dict[str, str]) -> str | None:
        role = attrs.get("role", "").strip().lower()
        if role in ROLE_LANDMARKS:
            return ROLE_LANDMARKS[role]
        if tag in TAG_LANDMARKS:
            return TAG_LANDMARKS[tag]
        if tag in _CONTAINER_TAGS:
            tokens = (attrs.get("id", "") + " " + attrs.get("class", "")).lower().split()
            for token in tokens:
                if token in CLASS_LANDMARKS:
                    return CLASS_LANDMARKS[token]
        return None

    def _position(self) -> str:
        landmarks = [landmark for _, landmark in self.stack if landmark is not None]
        if not landmarks:
            return "body"
        innermost = landmarks[-1]
        # <header>/<footer> inside <article>/<main> belong to the content, not the site chrome.
        if innermost in ("header", "footer") and "main" in landmarks[:-1]:
            return "main"
        return innermost

    def _close_anchor(self) -> None:
        if self.anchor is not None:
            self.raw_links.append(self.anchor)
            self.anchor = None

    def _close_h1(self) -> None:
        if self.h1_parts is not None:
            text = collapse_whitespace("".join(self.h1_parts))
            if text:
                self.h1_all.append(text)
            self.h1_parts = None

    # -- HTMLParser callbacks ---------------------------------------------

    def handle_starttag(self, tag: str, attr_list: list[tuple[str, str | None]]) -> None:
        attrs = {name: (value or "") for name, value in attr_list}
        if tag == "html" and not self.lang:
            self.lang = attrs.get("lang", "").strip()
        elif tag == "meta":
            self._handle_meta(attrs)
        elif tag == "link":
            rel = attrs.get("rel", "").lower().split()
            if "canonical" in rel and not self.canonical:
                self.canonical = attrs.get("href", "").strip()
        elif tag == "base":
            if not self.base_href and attrs.get("href"):
                self.base_href = attrs["href"].strip()
        elif tag == "img":
            alt = collapse_whitespace(attrs.get("alt", ""))
            if self.anchor is not None:
                self.anchor.has_image = True
                if alt:
                    self.anchor.alts.append(alt)
            if alt and self.h1_parts is not None:
                self.h1_parts.append(alt)
        elif tag == "a":
            self._close_anchor()
            if "href" in attrs:
                rel = tuple(dict.fromkeys(attrs.get("rel", "").lower().split()))
                label = attrs.get("aria-label", "") or attrs.get("title", "")
                fallback = collapse_whitespace(label)
                self.anchor = _OpenAnchor(attrs["href"], rel, self._position(), fallback)
        elif tag == "title" and not self.title_done and self.skip_depth == 0:
            self.in_title = True
        elif tag == "h1" and self.skip_depth == 0:
            self._close_h1()
            self.h1_parts = []

        if tag in _BLOCK_TAGS:
            self._break_text()
        if tag in VOID_ELEMENTS:
            return
        if tag in SKIP_TEXT_ELEMENTS:
            self.skip_depth += 1
        self.stack.append((tag, self._landmark(tag, attrs)))

    def handle_startendtag(self, tag: str, attr_list: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attr_list)
        if tag not in VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            return
        index = None
        for position in range(len(self.stack) - 1, -1, -1):
            if self.stack[position][0] == tag:
                index = position
                break
        if index is None:
            return
        closed = self.stack[index:]
        del self.stack[index:]
        for closed_tag, _ in closed:
            if closed_tag in SKIP_TEXT_ELEMENTS:
                self.skip_depth = max(0, self.skip_depth - 1)
            if closed_tag == "a":
                self._close_anchor()
            elif closed_tag == "title":
                if self.in_title:
                    self.in_title = False
                    self.title_done = True
            elif closed_tag == "h1":
                self._close_h1()
        if tag in _BLOCK_TAGS:
            self._break_text()

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.skip_depth:
            return
        self.text_parts.append(data)
        if self.anchor is not None:
            self.anchor.text.append(data)
        if self.h1_parts is not None:
            self.h1_parts.append(data)

    def close(self) -> None:
        super().close()
        self._close_anchor()
        self._close_h1()

    def _break_text(self) -> None:
        if self.text_parts and self.text_parts[-1] != " ":
            self.text_parts.append(" ")
        if self.anchor is not None:
            self.anchor.text.append(" ")
        if self.h1_parts is not None:
            self.h1_parts.append(" ")

    def _handle_meta(self, attrs: dict[str, str]) -> None:
        name = attrs.get("name", "").strip().lower()
        content = attrs.get("content", "").strip()
        if name in ("robots", "googlebot") and content:
            self.robots.append(content.lower())
        elif name == "description" and not self.description:
            self.description = collapse_whitespace(content)


def normalize_robots_directives(*values: str) -> str:
    """Merge robots directive strings into a de-duplicated, comma separated list."""
    seen: dict[str, None] = {}
    for value in values:
        for part in value.lower().split(","):
            part = part.strip()
            if part:
                seen.setdefault(part, None)
    return ",".join(seen)


def extract_page(
    html: str,
    base_url: str,
    options: NormalizeOptions = DEFAULT_OPTIONS,
    max_text_chars: int = 20000,
) -> PageExtract:
    """Parse ``html`` (the document at ``base_url``) into a :class:`PageExtract`.

    Link targets are resolved against the document URL (or ``<base href>``)
    and normalised; unsupported schemes such as ``mailto:`` are collected in
    ``skipped_hrefs`` instead.
    """
    parser = _Extractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - html.parser is very forgiving
        parser._close_anchor()
        parser._close_h1()

    base = base_url
    if parser.base_href:
        try:
            base = urljoin(base_url, parser.base_href)
        except ValueError:
            base = base_url

    result = PageExtract(
        title=collapse_whitespace("".join(parser.title_parts)),
        h1_all=parser.h1_all,
        h1=parser.h1_all[0] if parser.h1_all else "",
        meta_robots=normalize_robots_directives(*parser.robots),
        meta_description=parser.description,
        lang=parser.lang,
        base_href=parser.base_href,
    )
    if parser.canonical:
        result.canonical = normalize_url(parser.canonical, base=base, options=options) or ""

    for raw in parser.raw_links:
        href = raw.href.strip()
        url = normalize_url(href, base=base, options=options)
        if url is None:
            result.skipped_hrefs.append(href)
            continue
        text = collapse_whitespace("".join(raw.text))
        is_image = raw.has_image and not text
        anchor = text
        if not anchor and raw.alts:
            anchor = raw.alts[0]
        if not anchor:
            anchor = raw.fallback
        result.links.append(
            ExtractedLink(
                url=url,
                href=href,
                anchor=anchor,
                rel=raw.rel,
                position=raw.position,
                is_image=is_image,
            )
        )

    text = collapse_whitespace("".join(parser.text_parts))
    result.word_count = count_words(text)
    result.text = text[:max_text_chars] if max_text_chars >= 0 else text
    return result


def decode_body(body: bytes, content_type: str = "") -> str:
    """Decode an HTTP body using the header charset, a ``<meta>`` hint or UTF-8."""
    candidates: list[str] = []
    match = _HEADER_CHARSET_RE.search(content_type or "")
    if match:
        candidates.append(match.group(1))
    if body.startswith(b"\xef\xbb\xbf"):
        candidates.insert(0, "utf-8-sig")
    meta = _META_CHARSET_RE.search(body[:4096])
    if meta:
        candidates.append(meta.group(1).decode("ascii", "replace"))
    candidates.append("utf-8")
    for encoding in candidates:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")
