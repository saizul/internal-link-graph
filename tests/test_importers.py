import json

import pytest

from internal_link_graph.importers import (
    ImportFormatError,
    detect_format,
    guess_start_url,
    load_site,
    looks_like_screaming_frog,
    read_edge_csv,
    read_edge_json,
    read_screaming_frog_csv,
)
from internal_link_graph.models import Link, SiteData
from internal_link_graph.urlnorm import NormalizeOptions

SF_EXPORT = (
    '"Type","Source","Destination","Size (Bytes)","Alt Text","Anchor","Status Code","Status",'
    '"Follow","Target","Rel","Path Type","Link Position"\n'
    '"Hyperlink","https://example.com/","https://example.com/blog/","","","Blog","200","OK",'
    '"true","","","Root Relative","Navigation"\n'
    '"Hyperlink","https://example.com/","https://example.com/gone","","","Gone","404",'
    '"Not Found","true","","","Root Relative","Content"\n'
    '"Hyperlink","https://example.com/blog/","https://example.com/","","Logo","","200","OK",'
    '"false","","nofollow","Root Relative","Header"\n'
    '"Image","https://example.com/","https://example.com/logo.png","","Logo","","200","OK",'
    '"true","","","Root Relative","Header"\n'
    '"Hyperlink","https://example.com/","https://partner.example.org/","","","Partner","200",'
    '"OK","true","","","Absolute","Footer"\n'
    '"Hyperlink","https://example.com/","mailto:hi@example.com","","","Mail","0","","true","",'
    '"","Absolute","Footer"\n'
)


def test_edge_csv_with_header():
    site = read_edge_csv(
        "source,target,anchor,nofollow,position\n"
        "https://example.com/,https://example.com/a,Page A,,navigation\n"
        "https://example.com/a,https://example.com/,Home,yes,footer\n"
    )
    assert site.start_url == "https://example.com/"
    assert len(site.links) == 2 and set(site.pages) == {"https://example.com/",
                                                        "https://example.com/a"}
    first, second = site.links
    assert (first.anchor, first.position, first.nofollow) == ("Page A", "nav", False)
    assert (second.position, second.nofollow) == ("footer", True)
    assert site.meta["source"] == "edges"


def test_edge_csv_without_header_and_with_comments():
    site = read_edge_csv(
        "# exported by hand\n\n"
        "https://example.com/,https://example.com/a,Anchor text\n"
        "https://example.com/a,https://example.com/b\n"
    )
    assert len(site.links) == 2 and site.links[0].anchor == "Anchor text"


@pytest.mark.parametrize("delimiter", [";", "\t"])
def test_edge_csv_other_delimiters(delimiter):
    text = delimiter.join(["from", "to"]) + "\n" + delimiter.join(
        ["https://example.com/", "https://example.com/a"]
    )
    assert len(read_edge_csv(text).links) == 1


def test_edge_csv_normalises_urls_and_counts_skipped_rows():
    site = read_edge_csv(
        "source,target\n"
        "HTTPS://Example.com:443/a/../b?utm_source=x#top,https://example.com/c\n"
        "not-a-url,https://example.com/c\n"
        "https://example.com/c,mailto:someone@example.com\n"
    )
    assert site.links[0].source == "https://example.com/b"
    assert site.meta["skipped_rows"] == 2


def test_edge_csv_rel_column_marks_nofollow():
    site = read_edge_csv(
        "source,target,rel\nhttps://example.com/,https://example.com/a,sponsored noopener\n"
    )
    assert site.links[0].nofollow and site.links[0].rel == ("sponsored", "noopener")


def test_edge_csv_external_targets_are_links_but_not_pages():
    site = read_edge_csv(
        "source,target\nhttps://example.com/,https://example.com/a\n"
        "https://example.com/,https://other.example.org/x\n"
    )
    assert [link.internal for link in site.links] == [True, False]
    assert "https://other.example.org/x" not in site.pages


@pytest.mark.parametrize("text", ["", "# only a comment\n", "source,target\n", "justonecolumn\n",
                                  "source,target\nfoo,bar\n"])
def test_edge_csv_errors(text):
    with pytest.raises(ImportFormatError):
        read_edge_csv(text)


def test_explicit_start_url_and_invalid_start_url():
    text = "source,target\nhttps://example.com/x,https://example.com/y\n"
    assert read_edge_csv(text, start_url="https://example.com/y#f").start_url == (
        "https://example.com/y"
    )
    with pytest.raises(ImportFormatError):
        read_edge_csv(text, start_url="nope")


def test_normalisation_options_are_respected():
    options = NormalizeOptions(trailing_slash="strip")
    site = read_edge_csv("source,target\nhttps://example.com/a/,https://example.com/b/\n",
                         options=options)
    assert site.links[0].target == "https://example.com/b"


def test_guess_start_url_prefers_root_then_busiest_source():
    site = SiteData(links=[Link("https://example.com/a", "https://example.com/b"),
                           Link("https://example.com/", "https://example.com/a")])
    assert guess_start_url(site) == "https://example.com/"
    site = SiteData(links=[Link("https://example.com/a", "https://example.com/b"),
                           Link("https://example.com/a", "https://example.com/c"),
                           Link("https://example.com/b", "https://example.com/c")])
    assert guess_start_url(site) == "https://example.com/a"
    assert guess_start_url(SiteData()) == ""


def test_edge_json_pairs_and_objects():
    site = read_edge_json(
        [
            ["https://example.com/", "https://example.com/a", "A"],
            {"from": "https://example.com/a", "to": "https://example.com/", "text": "Home",
             "rel": ["nofollow"]},
            "garbage",
            ["only-one"],
        ]
    )
    assert [link.anchor for link in site.links] == ["A", "Home"]
    assert site.links[1].nofollow
    assert site.meta["skipped_rows"] == 2


def test_edge_json_object_with_nodes_and_sitemap():
    site = read_edge_json(
        {
            "start_url": "https://example.com/",
            "edges": [{"source": "https://example.com/", "target": "https://example.com/a"}],
            "nodes": [
                {"url": "https://example.com/a", "title": "Page A", "status": 200,
                 "word_count": "120", "text": "body"},
                "https://example.com/lonely",
                {"title": "no url"},
            ],
            "sitemap": ["https://example.com/lonely", "bad"],
        }
    )
    page = site.pages["https://example.com/a"]
    assert (page.title, page.status, page.word_count, page.text) == ("Page A", 200, 120, "body")
    assert site.pages["https://example.com/lonely"].in_sitemap
    assert site.sitemap_urls == {"https://example.com/lonely"}


@pytest.mark.parametrize("payload", ["text", 5, [], {"edges": []}, [["a", "b"]]])
def test_edge_json_errors(payload):
    with pytest.raises(ImportFormatError):
        read_edge_json(payload)


def test_screaming_frog_import():
    site = read_screaming_frog_csv(SF_EXPORT)
    assert site.meta["source"] == "screaming-frog"
    assert site.meta["skipped_rows"] == 2  # the image row and the mailto row
    assert site.start_url == "https://example.com/"
    assert len(site.links) == 4
    assert site.pages["https://example.com/gone"].status == 404
    assert site.pages["https://example.com/gone"].is_broken
    assert site.pages["https://example.com/"].status == 200
    by_target = {link.target: link for link in site.links}
    assert by_target["https://example.com/blog/"].position == "nav"
    assert by_target["https://example.com/gone"].position == "main"
    logo = by_target["https://example.com/"]
    assert logo.nofollow and logo.is_image and logo.anchor == "Logo"
    assert logo.position == "header"
    assert not by_target["https://partner.example.org/"].internal
    assert "https://partner.example.org/" not in site.pages


def test_screaming_frog_header_detection_and_errors():
    assert looks_like_screaming_frog(["Type", "Source", "Destination"])
    assert not looks_like_screaming_frog(["source", "target"])
    with pytest.raises(ImportFormatError):
        read_screaming_frog_csv("source,target\nhttps://example.com/,https://example.com/a\n")
    with pytest.raises(ImportFormatError):
        read_screaming_frog_csv(
            "Type,Source,Destination\nImage,https://example.com/,https://example.com/a.png\n"
        )


def test_detect_format(tmp_path):
    assert detect_format("x", '{"format": "internal-link-graph/crawl"}') == "crawl"
    assert detect_format("x", '[["https://example.com/", "https://example.com/a"]]') == "edges"
    assert detect_format("x", SF_EXPORT) == "screaming-frog"
    assert detect_format("x", "source,target\n") == "edges"
    with pytest.raises(ImportFormatError):
        detect_format("x", "{broken json")


def test_load_site_auto_detects_every_format(tmp_path):
    crawl = SiteData(start_url="https://example.com/")
    crawl.add_link(Link("https://example.com/", "https://example.com/a"))
    crawl.ensure_link_targets()
    crawl_path = tmp_path / "crawl.json"
    crawl.save(crawl_path)
    assert load_site(crawl_path).to_dict() == crawl.to_dict()
    assert load_site(crawl_path, start_url="https://example.com/a").start_url.endswith("/a")

    csv_path = tmp_path / "edges.csv"
    csv_path.write_text("source,target\nhttps://example.com/,https://example.com/a\n")
    assert len(load_site(csv_path).links) == 1

    json_path = tmp_path / "edges.json"
    json_path.write_text(json.dumps([["https://example.com/", "https://example.com/a"]]))
    assert len(load_site(json_path).links) == 1

    sf_path = tmp_path / "all_inlinks.csv"
    sf_path.write_text(SF_EXPORT, encoding="utf-8-sig")  # Screaming Frog writes a BOM
    assert load_site(sf_path).meta["source"] == "screaming-frog"
    assert len(load_site(sf_path, fmt="edges").links) >= 4  # forced generic parsing still works


def test_load_site_reads_utf16_exports(tmp_path):
    path = tmp_path / "edges.csv"
    path.write_text("source,target,anchor\nhttps://example.com/,https://example.com/a,গাইড\n",
                    encoding="utf-16")
    assert load_site(path).links[0].anchor == "গাইড"


def test_load_site_errors(tmp_path):
    with pytest.raises(ImportFormatError):
        load_site(tmp_path / "missing.csv")
    with pytest.raises(ImportFormatError):
        load_site(tmp_path, fmt="nonsense")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ImportFormatError):
        load_site(bad)
    with pytest.raises(ImportFormatError):
        load_site(bad, fmt="crawl")
    crawl = tmp_path / "crawl.json"
    crawl.write_text('{"format": "internal-link-graph/crawl", "pages": [], "links": []}')
    with pytest.raises(ImportFormatError):
        load_site(crawl, start_url="nope")


def test_example_edge_list_loads():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "examples" / "sample-edges.csv"
    site = load_site(example)
    assert site.start_url == "https://example.com/"
    assert len(site.links) == 29
