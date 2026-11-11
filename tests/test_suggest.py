import pytest

from conftest import BASE
from internal_link_graph.analyzer import analyze
from internal_link_graph.models import Link, SiteData
from internal_link_graph.suggest import (
    SuggestConfig,
    clean_title,
    document_tokens,
    slug_words,
    suggest_anchor,
    suggest_links,
)

HOME = f"{BASE}/"


def u(name: str) -> str:
    return f"{BASE}/{name}"


PAGES = {
    "": ("Garden blog", "Welcome to the garden blog with tips and guides."),
    "roses/pruning": ("Pruning roses in spring",
                      "Prune roses with clean secateurs. Rose bushes need pruning every spring."),
    "roses/diseases": ("Common rose diseases",
                       "Black spot and mildew damage rose bushes. Pruning roses helps airflow."),
    "roses/varieties": ("Climbing rose varieties",
                        "Climbing roses and shrub roses for every garden; rose care basics."),
    "compost/basics": ("Compost basics", "Compost kitchen scraps and garden waste in a bin."),
    "compost/worms": ("Worm composting", "Worms turn kitchen scraps into compost quickly."),
    "contact": ("Contact", "Send a message to the team."),
}


def make_site(extra_edges=()):
    site = SiteData(start_url=HOME)
    for path, (title, text) in PAGES.items():
        page = site.page(u(path))
        page.status, page.crawled, page.title, page.h1, page.text = 200, True, title, title, text
        page.content_type = "text/html"
    edges = [
        ("", "roses/pruning"), ("", "compost/basics"), ("", "contact"),
        ("roses/pruning", ""), ("compost/basics", ""), ("contact", ""),
        ("compost/basics", "compost/worms"), ("compost/worms", "compost/basics"),
        ("roses/varieties", "roses/pruning"),
        *extra_edges,
    ]
    for source, target in edges:
        site.add_link(Link(u(source), u(target), anchor=PAGES[target][0]))
    return site


def test_slug_words():
    assert slug_words(u("blog/pruning-roses_in+spring.html")) == "pruning roses in spring"
    assert slug_words(u("blog/%E0%A6%97%E0%A7%8B%E0%A6%B2%E0%A6%BE%E0%A6%AA/")) == "গোলাপ"
    assert slug_words(HOME) == ""


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Pruning roses | Example Garden", "Pruning roses"),
        ("EG - A very long descriptive page title", "A very long descriptive page title"),
        ("Plain title", "Plain title"),
        ("", ""),
    ],
)
def test_clean_title(title, expected):
    assert clean_title(title) == expected


def test_suggest_anchor_prefers_h1_then_title_then_slug():
    site = make_site()
    page = site.pages[u("roses/diseases")]
    assert suggest_anchor(page, page.url) == "Common rose diseases"
    page.h1 = ""
    page.title = "Rose disease guide | Example"
    assert suggest_anchor(page, page.url) == "Rose disease guide"
    assert suggest_anchor(None, u("blog/compost-bin-plans")) == "Compost bin plans"
    long_page = site.pages[u("contact")]
    long_page.h1 = "one two three four five six seven eight nine ten"
    assert len(suggest_anchor(long_page, long_page.url, max_words=4).split()) <= 5


def test_document_tokens_weight_slug_and_title():
    site = make_site()
    config = SuggestConfig()
    tokens = document_tokens(site.pages[u("compost/worms")], u("compost/worms"), config)
    assert tokens.count("worms") >= config.slug_weight
    assert "the" not in tokens
    assert document_tokens(None, u("roses/pruning-guide"), config).count("pruning") == 2


def test_orphan_gets_suggestion_from_topically_related_page():
    site = make_site()
    suggestions = suggest_links(site, config=SuggestConfig(min_similarity=0.05))
    orphan = [s for s in suggestions if s.target == u("roses/diseases")]
    assert orphan, "the orphaned rose page should receive a suggestion"
    best = orphan[0]
    assert best.source in {u("roses/pruning"), u("roses/varieties")}
    assert best.target_inlinks == 0 and best.target_depth is None
    assert best.anchor == "Common rose diseases"
    assert any(term.startswith("rose") for term in best.shared_terms)
    assert "no internal inlinks" in best.reason or "not reachable" in best.reason
    assert 0 < best.similarity <= 1.0 and best.score > 0


def test_existing_links_are_never_suggested():
    site = make_site()
    existing = {(link.source, link.target) for link in site.links}
    suggestions = suggest_links(site, config=SuggestConfig(min_similarity=0.0))
    assert suggestions
    assert not existing & {(s.source, s.target) for s in suggestions}
    assert all(s.source != s.target for s in suggestions)
    assert all(s.target != HOME for s in suggestions)


def test_suggestions_are_sorted_and_limited():
    site = make_site()
    config = SuggestConfig(min_similarity=0.0, max_suggestions=3, per_target=1, per_source=5)
    suggestions = suggest_links(site, config=config)
    assert len(suggestions) <= 3
    assert [s.score for s in suggestions] == sorted((s.score for s in suggestions), reverse=True)
    targets = [s.target for s in suggestions]
    assert len(targets) == len(set(targets))


def test_per_source_limit():
    site = make_site()
    config = SuggestConfig(min_similarity=0.0, per_source=1, per_target=10)
    sources = [s.source for s in suggest_links(site, config=config)]
    assert len(sources) == len(set(sources))


def test_min_similarity_filters_unrelated_pairs():
    site = make_site()
    strict = suggest_links(site, config=SuggestConfig(min_similarity=0.99))
    assert strict == []
    loose = suggest_links(site, config=SuggestConfig(min_similarity=0.05))
    assert all(s.similarity >= 0.05 for s in loose)
    pairs = {(s.source, s.target) for s in loose}
    assert (u("compost/worms"), u("roses/diseases")) not in pairs


def test_well_linked_shallow_pages_are_not_targets():
    site = make_site(extra_edges=[("contact", "roses/pruning"), ("compost/basics",
                                                                 "roses/pruning")])
    config = SuggestConfig(min_similarity=0.0, max_inlinks=1, min_depth=3)
    targets = {s.target for s in suggest_links(site, config=config)}
    assert u("roses/pruning") not in targets
    assert u("roses/diseases") in targets


def test_noindex_and_broken_pages_are_excluded():
    site = make_site()
    site.pages[u("roses/diseases")].meta_robots = "noindex"
    site.pages[u("roses/varieties")].status = 404
    suggestions = suggest_links(site, config=SuggestConfig(min_similarity=0.0))
    urls = {s.source for s in suggestions} | {s.target for s in suggestions}
    assert u("roses/diseases") not in urls and u("roses/varieties") not in urls


def test_overloaded_sources_are_skipped():
    site = make_site()
    config = SuggestConfig(min_similarity=0.0, max_source_outlinks=2)
    assert HOME not in {s.source for s in suggest_links(site, config=config)}


def test_precomputed_analysis_is_reused_and_results_are_deterministic():
    site = make_site()
    analysis = analyze(site)
    config = SuggestConfig(min_similarity=0.05)
    first = [s.to_dict() for s in suggest_links(site, analysis, config)]
    second = [s.to_dict() for s in suggest_links(site, config=config)]
    assert first == second and first
    assert {"source", "target", "score", "anchor", "reason"} <= set(first[0])


def test_tiny_sites_yield_no_suggestions():
    site = SiteData(start_url=HOME)
    site.page(HOME)
    assert suggest_links(site) == []


def test_bangla_pages_need_unicode_tokens():
    site = SiteData(start_url=HOME)
    texts = {
        "": ("হোম", "স্বাগতম"),
        "golap-jotno": ("গোলাপ গাছের যত্ন", "গোলাপ গাছের যত্ন নেওয়ার সহজ নিয়ম এবং গোলাপ ছাঁটাই"),
        "golap-rog": ("গোলাপ গাছের রোগ", "গোলাপ গাছের রোগ এবং প্রতিকার, গোলাপ পাতায় দাগ"),
        "sar": ("জৈব সার", "রান্নাঘরের বর্জ্য থেকে জৈব সার তৈরি"),
    }
    for path, (title, text) in texts.items():
        page = site.page(u(path))
        page.status, page.crawled, page.title, page.h1, page.text = 200, True, title, title, text
    for target in ("golap-jotno", "sar"):
        site.add_link(Link(HOME, u(target), anchor=texts[target][0]))
        site.add_link(Link(u(target), HOME, anchor="হোম"))
    config = SuggestConfig(min_similarity=0.1, unicode_tokens=True)
    suggestions = suggest_links(site, config=config)
    rog = [s for s in suggestions if s.target == u("golap-rog")]
    assert rog and rog[0].source == u("golap-jotno")
    assert rog[0].anchor == "গোলাপ গাছের রোগ"
    ascii_only = suggest_links(site, config=SuggestConfig(min_similarity=0.5))
    assert not [s for s in ascii_only if s.target == u("golap-rog")
                and s.source == u("golap-jotno") and s.similarity > 0.9]
