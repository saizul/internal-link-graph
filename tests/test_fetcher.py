import gzip
import io
import urllib.error
import zlib

import pytest

from internal_link_graph.fetcher import DictFetcher, Response, UrllibFetcher, fetch_following


class FakeHandle:
    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self._body = body

    def read(self, amount=-1):
        return self._body if amount < 0 else self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, result):
        self.result = result
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_response_normalises_headers_and_body():
    response = Response("u", headers={"Content-Type": "Text/HTML; charset=utf-8"}, body="hé")
    assert response.header("CONTENT-TYPE").startswith("Text/HTML")
    assert response.content_type == "text/html"
    assert response.body == "hé".encode()
    assert response.is_html and response.ok


def test_response_sniffs_html_without_content_type():
    assert Response("u", body=b"  <!DOCTYPE html><html>").is_html
    assert not Response("u", body=b"%PDF-1.7").is_html
    assert not Response("u", headers={"content-type": "application/pdf"}, body=b"<html>").is_html


def test_response_redirect_properties():
    response = Response("u", status=302, headers={"Location": " /next "})
    assert response.is_redirect and response.location == "/next"
    assert not Response("u", status=304).is_redirect


def test_dict_fetcher_entry_types():
    def dynamic(url):
        return Response(url, status=418)

    fetcher = DictFetcher(
        {
            "s": "<html>hi</html>",
            "b": b"<html>bytes</html>",
            "i": 410,
            "t3": (503, {"Retry-After": "5"}, "busy"),
            "t2": (200, "two"),
            "r": Response("ignored", status=201, body=b"x"),
            "c": dynamic,
        }
    )
    assert fetcher.fetch("s").status == 200 and fetcher.fetch("s").is_html
    assert fetcher.fetch("b").body == b"<html>bytes</html>"
    assert fetcher.fetch("i").status == 410
    assert fetcher.fetch("t3").header("retry-after") == "5"
    assert fetcher.fetch("t2").body == b"two"
    assert fetcher.fetch("r").status == 201 and fetcher.fetch("r").url == "r"
    assert fetcher.fetch("c").status == 418
    assert fetcher.fetch("missing").status == 404
    assert fetcher.count("s") == 2


def test_dict_fetcher_rejects_bad_tuples():
    with pytest.raises(ValueError):
        DictFetcher({"x": (1, 2, 3, 4)}).fetch("x")


def test_dict_fetcher_redirect_and_add():
    fetcher = DictFetcher()
    fetcher.redirect("a", "b", status=308)
    fetcher.add("b", "done")
    response = fetcher.fetch("a")
    assert response.status == 308 and response.location == "b"


def test_fetch_following_resolves_relative_hops():
    fetcher = DictFetcher(
        {
            "https://example.com/a": (301, {"Location": "/b"}, ""),
            "https://example.com/b": (302, {"Location": "c"}, ""),
            "https://example.com/c": "final",
        }
    )
    response, hops = fetch_following(fetcher, "https://example.com/a")
    assert response.body == b"final"
    assert hops == ["https://example.com/a", "https://example.com/b", "https://example.com/c"]


def test_fetch_following_stops_on_loops_and_limits():
    loop = DictFetcher(
        {"https://example.com/a": (301, {"Location": "/b"}, ""),
         "https://example.com/b": (301, {"Location": "/a"}, "")}
    )
    response, hops = fetch_following(loop, "https://example.com/a")
    assert response.is_redirect and len(hops) == 2
    chain = DictFetcher(
        {f"https://example.com/{i}": (301, {"Location": f"/{i + 1}"}, "") for i in range(10)}
    )
    _, hops = fetch_following(chain, "https://example.com/0", max_redirects=3)
    assert len(hops) == 4


def test_urllib_fetcher_success_sends_headers_and_timeout():
    opener = FakeOpener(FakeHandle(200, {"Content-Type": "text/html"}, b"<html>ok</html>"))
    fetcher = UrllibFetcher(user_agent="test-agent/1.0", timeout=3, opener=opener,
                            extra_headers={"X-Test": "1"})
    response = fetcher.fetch("https://example.com/")
    assert response.status == 200 and response.body == b"<html>ok</html>"
    request = opener.requests[0]
    assert request.get_header("User-agent") == "test-agent/1.0"
    assert request.get_header("X-test") == "1"
    assert opener.timeouts == [3]


def test_urllib_fetcher_decompresses_gzip_and_deflate():
    payload = b"<html>compressed</html>"
    gz = FakeOpener(FakeHandle(200, {"Content-Encoding": "gzip"}, gzip.compress(payload)))
    assert UrllibFetcher(opener=gz).fetch("https://example.com/").body == payload
    deflated = FakeOpener(FakeHandle(200, {"Content-Encoding": "deflate"}, zlib.compress(payload)))
    assert UrllibFetcher(opener=deflated).fetch("https://example.com/").body == payload
    broken = FakeOpener(FakeHandle(200, {"Content-Encoding": "gzip"}, b"not gzip"))
    assert UrllibFetcher(opener=broken).fetch("https://example.com/").body == b"not gzip"


def test_urllib_fetcher_truncates_large_bodies():
    opener = FakeOpener(FakeHandle(200, {}, b"x" * 100))
    response = UrllibFetcher(opener=opener, max_bytes=10).fetch("https://example.com/")
    assert response.body == b"x" * 10


def test_urllib_fetcher_returns_http_errors_as_responses():
    error = urllib.error.HTTPError(
        "https://example.com/gone", 404, "Not Found", {"Content-Type": "text/html"},
        io.BytesIO(b"<html>missing</html>"),
    )
    response = UrllibFetcher(opener=FakeOpener(error)).fetch("https://example.com/gone")
    assert response.status == 404 and b"missing" in response.body


def test_urllib_fetcher_redirect_is_reported_not_followed():
    error = urllib.error.HTTPError(
        "https://example.com/old", 301, "Moved", {"Location": "/new"}, io.BytesIO(b"")
    )
    response = UrllibFetcher(opener=FakeOpener(error)).fetch("https://example.com/old")
    assert response.is_redirect and response.location == "/new"


@pytest.mark.parametrize(
    ("exc", "text"),
    [
        (urllib.error.URLError("name resolution failed"), "connection error"),
        (TimeoutError("slow"), "timeout"),
        (ConnectionResetError("reset"), "ConnectionResetError"),
    ],
)
def test_urllib_fetcher_network_failures(exc, text):
    response = UrllibFetcher(opener=FakeOpener(exc)).fetch("https://example.com/")
    assert response.status == 0
    assert text in response.error
