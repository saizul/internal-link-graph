import pytest

from internal_link_graph.html_extract import (
    collapse_whitespace,
    decode_body,
    extract_page,
    normalize_robots_directives,
)
from internal_link_graph.urlnorm import NormalizeOptions

URL = "https://example.com/dir/page"


def links_of(html: str, **kwargs):
    return extract_page(html, URL, **kwargs).links


def test_title_h1_and_word_count():
    page = extract_page(
        "<html><head><title> My  Page </title></head>"
        "<body><h1>Main <em>heading</em></h1><p>Three more words.</p><h1>Second</h1></body></html>",
        URL,
    )
    assert page.title == "My Page"
    assert page.h1 == "Main heading"
    assert page.h1_all == ["Main heading", "Second"]
    assert page.word_count == 6
    assert "Three more words." in page.text


def test_script_style_and_noscript_text_is_ignored():
    page = extract_page(
        "<body><script>var hidden = 1;</script><style>.x{color:red}</style>"
        "<noscript>fallback</noscript><p>visible text</p></body>",
        URL,
    )
    assert page.text == "visible text"
    assert page.word_count == 2


def test_title_inside_svg_is_not_the_page_title():
    page = extract_page(
        "<head><title>Real</title></head><body><svg><title>Icon</title></svg></body>", URL
    )
    assert page.title == "Real"


def test_links_are_resolved_and_normalised():
    found = links_of('<a href="../other?utm_source=x#frag">Other</a><a href="/abs/">Abs</a>')
    assert [link.url for link in found] == ["https://example.com/other", "https://example.com/abs/"]
    assert found[0].href == "../other?utm_source=x#frag"
    assert found[0].anchor == "Other"


def test_base_href_changes_resolution():
    found = links_of('<head><base href="https://example.com/root/"></head><a href="x">X</a>')
    assert found[0].url == "https://example.com/root/x"


def test_non_http_links_are_skipped_and_reported():
    page = extract_page(
        '<a href="mailto:a@example.com">Mail</a><a href="javascript:void(0)">JS</a>'
        '<a href="tel:123">Call</a><a href="/ok">OK</a><a>no href</a>',
        URL,
    )
    assert [link.url for link in page.links] == ["https://example.com/ok"]
    assert page.skipped_hrefs == ["mailto:a@example.com", "javascript:void(0)", "tel:123"]


def test_anchor_text_whitespace_and_nested_markup():
    found = links_of('<a href="/a">\n  Read   <strong>this</strong>\n guide </a>')
    assert found[0].anchor == "Read this guide"


@pytest.mark.parametrize(
    ("rel", "nofollow"),
    [("nofollow", True), ("UGC", True), ("sponsored noopener", True), ("noopener", False),
     ("", False)],
)
def test_rel_values(rel, nofollow):
    found = links_of(f'<a href="/a" rel="{rel}">A</a>')
    assert found[0].nofollow is nofollow
    assert found[0].rel == tuple(rel.lower().split())


def test_image_link_uses_alt_text_as_anchor():
    found = links_of('<a href="/a"><img src="x.png" alt="Product photo"></a>')
    assert found[0].anchor == "Product photo"
    assert found[0].is_image is True


def test_image_with_text_is_not_an_image_link():
    found = links_of('<a href="/a"><img src="x.png" alt="Logo"> Home</a>')
    assert found[0].anchor == "Home"
    assert found[0].is_image is False


def test_image_without_alt_has_empty_anchor():
    found = links_of('<a href="/a"><img src="x.png"></a>')
    assert found[0].anchor == ""
    assert found[0].is_image is True


def test_aria_label_and_title_fallbacks():
    found = links_of(
        '<a href="/a" aria-label="Open menu"></a><a href="/b" title="Tooltip"></a>'
        '<a href="/c" aria-label="ignored">Visible</a>'
    )
    assert [link.anchor for link in found] == ["Open menu", "Tooltip", "Visible"]


@pytest.mark.parametrize(
    ("html", "position"),
    [
        ('<nav><a href="/a">A</a></nav>', "nav"),
        ('<header><a href="/a">A</a></header>', "header"),
        ('<footer><a href="/a">A</a></footer>', "footer"),
        ('<main><a href="/a">A</a></main>', "main"),
        ('<article><p><a href="/a">A</a></p></article>', "main"),
        ('<aside><a href="/a">A</a></aside>', "aside"),
        ('<div role="navigation"><a href="/a">A</a></div>', "nav"),
        ('<div role="contentinfo"><a href="/a">A</a></div>', "footer"),
        ('<div class="site-footer"><a href="/a">A</a></div>', "footer"),
        ('<div id="sidebar"><a href="/a">A</a></div>', "aside"),
        ('<ul class="menu primary"><li><a href="/a">A</a></li></ul>', "nav"),
        ('<p><a href="/a">A</a></p>', "body"),
    ],
)
def test_link_position(html, position):
    assert links_of(html)[0].position == position


def test_innermost_landmark_wins():
    found = links_of('<header><nav><a href="/a">A</a></nav><a href="/b">B</a></header>')
    assert [link.position for link in found] == ["nav", "header"]


def test_header_inside_article_counts_as_main():
    found = links_of('<article><header><a href="/a">A</a></header></article>')
    assert found[0].position == "main"


def test_position_resets_after_landmark_closes():
    found = links_of('<nav><a href="/a">A</a></nav><p><a href="/b">B</a></p>')
    assert [link.position for link in found] == ["nav", "body"]


def test_canonical_and_meta_robots():
    page = extract_page(
        '<head><link rel="canonical" href="/canonical?utm_medium=x">'
        '<meta name="robots" content="NOINDEX, follow">'
        '<meta name="googlebot" content="nofollow">'
        '<meta name="description" content="A description."></head>',
        URL,
    )
    assert page.canonical == "https://example.com/canonical"
    assert "noindex" in page.meta_robots.split(",")
    assert page.meta_description == "A description."


def test_lang_attribute():
    assert extract_page('<html lang="bn-BD"><body>x</body></html>', URL).lang == "bn-BD"


def test_unclosed_anchor_is_still_recorded():
    found = links_of('<p><a href="/a">Unclosed <a href="/b">Second</a></p>')
    assert [link.url for link in found] == ["https://example.com/a", "https://example.com/b"]
    assert found[0].anchor == "Unclosed"


def test_broken_markup_does_not_raise():
    page = extract_page("<html><body><div><a href='/x'>x</div></span><<<>>>&&& <p", URL)
    assert page.links[0].url == "https://example.com/x"


def test_entities_are_decoded():
    found = links_of('<a href="/a?x=1&amp;y=2">Tom &amp; Jerry &#8211; fun</a>')
    assert found[0].url == "https://example.com/a?x=1&y=2"
    assert found[0].anchor == "Tom & Jerry – fun"


def test_max_text_chars_truncates_text_but_not_word_count():
    page = extract_page("<p>" + "word " * 100 + "</p>", URL, max_text_chars=20)
    assert len(page.text) == 20
    assert page.word_count == 100


def test_normalisation_options_are_applied_to_links():
    options = NormalizeOptions(trailing_slash="strip")
    assert links_of('<a href="/a/">A</a>', options=options)[0].url == "https://example.com/a"


def test_bangla_text_is_counted():
    page = extract_page("<title>এসইও গাইড</title><p>অভ্যন্তরীণ লিংক গুরুত্বপূর্ণ</p>", URL)
    assert page.title == "এসইও গাইড"
    assert page.word_count == 3


def test_normalize_robots_directives_and_whitespace():
    assert normalize_robots_directives("NoIndex, follow", "noindex", "") == "noindex,follow"
    assert collapse_whitespace("  a \n\t b  ") == "a b"


def test_decode_body_charsets():
    assert decode_body("café".encode("latin-1"), "text/html; charset=ISO-8859-1") == "café"
    assert decode_body(b"\xef\xbb\xbfhello") == "hello"
    assert decode_body('<meta charset="windows-1252">caf\xe9'.encode("cp1252")).endswith("café")
    assert decode_body("বাংলা".encode()) == "বাংলা"
    assert decode_body(b"\xff\xfe bad", "text/html; charset=utf-8")  # never raises
    assert decode_body(b"abc", "text/html; charset=not-a-real-codec") == "abc"
