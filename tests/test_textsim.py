import math

import pytest

from internal_link_graph.textsim import (
    BANGLA_STOPWORDS,
    ENGLISH_STOPWORDS,
    TfidfIndex,
    cosine_similarity,
    count_words,
    l2_normalize,
    split_unicode_words,
    tokenize,
    tokenize_ascii,
    tokenize_unicode,
)


def test_tokenize_drops_stopwords_short_tokens_and_numbers():
    assert tokenize("The 10 best Roses of 2024 and a x") == ["best", "roses"]


def test_tokenize_lowercases_and_splits_punctuation():
    assert tokenize("Internal-Linking: Guide!") == ["internal", "linking", "guide"]


def test_tokenize_custom_stopwords_and_min_length():
    assert tokenize("go to it", stopwords=frozenset(), min_length=1) == ["go", "to", "it"]


def test_ascii_tokenizer_ignores_non_latin():
    assert tokenize_ascii("seo গাইড guide") == ["seo", "guide"]


def test_unicode_tokenizer_keeps_bangla_words_intact():
    words = tokenize_unicode("এসইও গাইড: অভ্যন্তরীণ লিংক")
    assert words == ["এসইও", "গাইড", "অভ্যন্তরীণ", "লিংক"]


def test_unicode_tokenizer_keeps_combining_marks_and_joiners():
    word = "ক্‍ষ"  # consonant + virama + ZWJ + consonant
    assert split_unicode_words(f"{word} test") == [word, "test"]


def test_tokenize_unicode_mode_removes_bangla_stopwords():
    tokens = tokenize("এসইও এবং লিংক", unicode_letters=True)
    assert tokens == ["এসইও", "লিংক"]
    assert "এবং" in BANGLA_STOPWORDS


def test_stopword_sets_are_disjoint_scripts():
    assert "the" in ENGLISH_STOPWORDS
    assert all(not word.isascii() for word in BANGLA_STOPWORDS)


def test_count_words_handles_mixed_scripts():
    assert count_words("Hello world, এটি একটি test.") == 5
    assert count_words("") == 0


def test_l2_normalize():
    vector = l2_normalize({"a": 3.0, "b": 4.0})
    assert vector == pytest.approx({"a": 0.6, "b": 0.8})
    assert l2_normalize({}) == {}
    assert l2_normalize({"a": 0.0}) == {"a": 0.0} or l2_normalize({"a": 0.0}) == {}


def test_cosine_similarity_basic():
    a = l2_normalize({"x": 1.0, "y": 1.0})
    b = l2_normalize({"x": 1.0})
    assert cosine_similarity(a, a) == pytest.approx(1.0)
    assert cosine_similarity(a, b) == pytest.approx(1 / math.sqrt(2))
    assert cosine_similarity(a, {"z": 1.0}) == 0.0
    assert cosine_similarity({}, a) == 0.0


def make_index() -> TfidfIndex:
    return TfidfIndex(
        {
            "roses": ["rose", "pruning", "rose", "garden"],
            "disease": ["rose", "disease", "mildew"],
            "compost": ["compost", "garden", "waste"],
            "empty": [],
        }
    )


def test_idf_formula_matches_hand_computation():
    index = make_index()
    # 4 documents; "rose" appears in 2, "mildew" in 1.
    assert index.idf["rose"] == pytest.approx(math.log(5 / 3) + 1)
    assert index.idf["mildew"] == pytest.approx(math.log(5 / 2) + 1)


def test_vector_weights_match_hand_computation():
    index = make_index()
    idf_rose = math.log(5 / 3) + 1
    idf_pruning = math.log(5 / 2) + 1
    idf_garden = math.log(5 / 3) + 1
    raw = {
        "rose": (1 + math.log(2)) * idf_rose,
        "pruning": idf_pruning,
        "garden": idf_garden,
    }
    norm = math.sqrt(sum(value * value for value in raw.values()))
    expected = {term: value / norm for term, value in raw.items()}
    assert index.vector("roses") == pytest.approx(expected)


def test_vectors_are_unit_length_and_empty_doc_is_empty():
    index = make_index()
    assert sum(v * v for v in index.vector("disease").values()) == pytest.approx(1.0)
    assert index.vector("empty") == {}
    assert index.vector("unknown") == {}


def test_similarity_is_symmetric_and_ranks_related_documents_higher():
    index = make_index()
    assert index.similarity("roses", "disease") == pytest.approx(
        index.similarity("disease", "roses")
    )
    assert index.similarity("roses", "disease") > index.similarity("disease", "compost")
    assert index.similarity("disease", "compost") == 0.0
    assert index.similarity("roses", "roses") == pytest.approx(1.0)


def test_most_similar_orders_filters_and_limits():
    index = make_index()
    ranked = index.most_similar("roses")
    assert [doc for doc, _ in ranked][0] in {"disease", "compost"}
    assert all(score > 0 for _, score in ranked)
    assert "roses" not in [doc for doc, _ in ranked]
    assert len(index.most_similar("roses", limit=1)) == 1
    assert index.most_similar("roses", candidates=["compost"])[0][0] == "compost"
    assert index.most_similar("roses", min_similarity=0.99) == []
    assert len(index.most_similar("roses", limit=0)) == 2


def test_shared_and_top_terms():
    index = make_index()
    assert index.shared_terms("roses", "disease") == ["rose"]
    assert index.shared_terms("disease", "compost") == []
    assert index.top_terms("disease", 1)[0] in {"disease", "mildew"}


def test_query_and_transform():
    index = make_index()
    results = index.query(index.transform(["mildew"]))
    assert results[0][0] == "disease"
    assert index.transform(["neverseen"]) == {}
    assert index.query({}) == []
    assert index.query(index.transform(["rose"]), exclude={"roses"})[0][0] == "disease"
