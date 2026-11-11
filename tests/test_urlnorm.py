import pytest

from internal_link_graph.urlnorm import (
    NormalizeOptions,
    host_of,
    is_internal,
    is_tracking_param,
    normalize_percent_encoding,
    normalize_url,
    origin_of,
    path_segments,
    remove_dot_segments,
    same_site,
    short_path,
    url_directory,
    url_extension,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTP://Example.COM/Path", "http://example.com/Path"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a?utm_source=x&id=2", "https://example.com/a?id=2"),
        ("https://example.com/a?fbclid=abc", "https://example.com/a"),
        ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
        ("https://example.com/a/../b/./c", "https://example.com/b/c"),
        ("https://example.com//a///b", "https://example.com/a/b"),
        ("https://example.com/%7Euser", "https://example.com/~user"),
        ("https://example.com/a%2fb", "https://example.com/a%2Fb"),
        ("https://example.com/a b", "https://example.com/a%20b"),
        ("https://example.com./a", "https://example.com/a"),
        ("  https://example.com/a  ", "https://example.com/a"),
    ],
)
def test_normalize_url_defaults(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["mailto:hi@example.com", "javascript:void(0)", "tel:+880123", "", "#top", "data:text/plain,x",
     "ftp://example.com/file"],
)
def test_normalize_url_rejects_non_http(raw):
    assert normalize_url(raw) is None


def test_fragment_only_link_resolves_to_base_page():
    assert normalize_url("#top", base="https://example.com/a") == "https://example.com/a"


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("b", "https://example.com/dir/b"),
        ("/b", "https://example.com/b"),
        ("../b", "https://example.com/b"),
        ("//cdn.example.com/x", "https://cdn.example.com/x"),
        ("?page=2", "https://example.com/dir/a?page=2"),
        ("https://other.example.org/", "https://other.example.org/"),
    ],
)
def test_normalize_url_resolves_relative(href, expected):
    assert normalize_url(href, base="https://example.com/dir/a") == expected


def test_trailing_slash_policies():
    assert normalize_url("https://example.com/a/") == "https://example.com/a/"
    strip = NormalizeOptions(trailing_slash="strip")
    assert normalize_url("https://example.com/a/", options=strip) == "https://example.com/a"
    assert normalize_url("https://example.com/", options=strip) == "https://example.com/"
    add = NormalizeOptions(trailing_slash="add")
    assert normalize_url("https://example.com/a", options=add) == "https://example.com/a/"
    assert normalize_url("https://example.com/a.html", options=add) == "https://example.com/a.html"


def test_invalid_trailing_slash_policy():
    with pytest.raises(ValueError):
        NormalizeOptions(trailing_slash="sometimes")


def test_keep_tracking_and_unsorted_query():
    options = NormalizeOptions(strip_tracking=False, sort_query=False)
    url = "https://example.com/a?utm_source=x&b=1&a=2"
    assert normalize_url(url, options=options) == url


def test_extra_tracking_params():
    options = NormalizeOptions(extra_tracking_params=frozenset({"sessionid"}))
    assert normalize_url("https://example.com/?sessionid=1&q=x", options=options) == (
        "https://example.com/?q=x"
    )


def test_strip_query_www_index_and_force_https():
    options = NormalizeOptions(strip_query=True, strip_www=True, strip_index=True,
                               force_https=True)
    assert normalize_url("http://www.example.com/blog/index.html?x=1", options=options) == (
        "https://example.com/blog/"
    )


def test_blank_query_values_are_preserved():
    assert normalize_url("https://example.com/?q=") == "https://example.com/?q="


def test_idn_host_is_punycoded():
    assert normalize_url("https://bücher.example/") == "https://xn--bcher-kva.example/"


def test_userinfo_is_dropped_or_kept_consistently():
    result = normalize_url("https://user:pw@example.com/a")
    assert result is not None and result.endswith("example.com/a")


def test_normalize_percent_encoding_cases():
    assert normalize_percent_encoding("%41%2f%7e", "/") == "A%2F~"
    assert normalize_percent_encoding("100%", "/") == "100%25"
    assert normalize_percent_encoding("বাংলা", "/").startswith("%E0%A6")


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/a/b/../c", "/a/c"), ("/../a", "/a"), ("/a/./b/", "/a/b/"), ("/a/b/..", "/a/"), ("/", "/")],
)
def test_remove_dot_segments(path, expected):
    assert remove_dot_segments(path) == expected


def test_is_tracking_param():
    assert is_tracking_param("utm_campaign")
    assert is_tracking_param("GCLID")
    assert not is_tracking_param("page")


def test_host_and_origin():
    assert host_of("https://Example.com:8443/a") == "example.com"
    assert origin_of("https://example.com/a/b?c") == "https://example.com"
    assert host_of("not a url") == ""


def test_same_site_and_is_internal():
    assert same_site("www.example.com", "example.com")
    assert same_site("EXAMPLE.com.", "example.com")
    assert not same_site("blog.example.com", "example.com")
    assert same_site("blog.example.com", "example.com", include_subdomains=True)
    assert not same_site("badexample.com", "example.com", include_subdomains=True)
    assert not same_site("", "example.com")
    assert is_internal("https://example.com/a", "https://www.example.com/")
    assert not is_internal("https://example.org/a", "https://example.com/")
    assert is_internal("https://shop.example.com/a", "https://example.com/", True)


def test_path_helpers():
    assert path_segments("https://example.com/a/b/") == ["a", "b"]
    assert url_extension("https://example.com/a/file.PDF?x=1") == "pdf"
    assert url_extension("https://example.com/a/") == ""


@pytest.mark.parametrize(
    ("url", "depth", "expected"),
    [
        ("https://example.com/", 1, "/"),
        ("https://example.com/about", 1, "/"),
        ("https://example.com/blog/post", 1, "/blog/"),
        ("https://example.com/blog/seo/post", 1, "/blog/"),
        ("https://example.com/blog/seo/post", 2, "/blog/seo/"),
        ("https://example.com/blog/seo/", 2, "/blog/seo/"),
        ("https://example.com/blog/post", 0, "/"),
    ],
)
def test_url_directory(url, depth, expected):
    assert url_directory(url, depth) == expected


def test_short_path():
    assert short_path("https://example.com/a?b=1") == "/a?b=1"
    long_url = "https://example.com/" + "x" * 200
    shortened = short_path(long_url, 40)
    assert len(shortened) <= 40 and "..." in shortened
