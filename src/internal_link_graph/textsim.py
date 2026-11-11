"""Tokenisation and a small TF-IDF / cosine similarity engine.

Everything here is implemented from scratch on top of the standard library so
that the package stays dependency-free.  The Unicode tokeniser is "Bangla
safe": Python's ``\\w`` does not match combining vowel signs or the virama, so
a naive regular expression chops Bangla (and most other Indic) words into
fragments.  :func:`tokenize_unicode` keeps letters, combining marks and digits
together instead.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping

ENGLISH_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are aren as at be because been
    before being below between both but by can cannot could couldn did didn do does doesn doing
    don down during each few for from further get got had hadn has hasn have haven having he her
    here hers herself him himself his how however i if in into is isn it its itself just ll me
    more most mustn my myself no nor not now of off on once only or other our ours ourselves out
    over own re s same shan she should shouldn so some such t than that the their theirs them
    themselves then there these they this those through to too under until up us ve very was
    wasn we were weren what when where which while who whom why will with won would wouldn you
    your yours yourself yourselves via per vs etc www com http https html htm php
    """.split()
)

BANGLA_STOPWORDS = frozenset(
    """
    এবং ও এই যে করে থেকে জন্য একটি না হয় কি তার সে এর বা তা আর কিন্তু যা এক হয়ে করা করতে হবে
    সঙ্গে সাথে মধ্যে উপর পর আমি আমরা তুমি আপনি তারা ছিল আছে নয় নেই কোন কোনো যদি তবে তাই এ
    হলো হল দিয়ে নিয়ে কিছু সব অনেক খুব আরও
    """.split()
)

DEFAULT_STOPWORDS = ENGLISH_STOPWORDS | BANGLA_STOPWORDS

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")
_JOINERS = frozenset("‌‍")


def _is_word_char(ch: str) -> bool:
    if ch in _JOINERS:
        return True
    category = unicodedata.category(ch)
    return category[0] in ("L", "M", "N")


def split_unicode_words(text: str) -> list[str]:
    """Split text into words made of Unicode letters, marks and digits.

    Zero-width joiners are kept inside a word but never start one.
    """
    words: list[str] = []
    current: list[str] = []
    for ch in text:
        if ch.isascii():
            is_word = ch.isalnum()
        else:
            is_word = _is_word_char(ch)
        if is_word:
            if not current and ch in _JOINERS:
                continue
            current.append(ch)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return [word.rstrip("‌‍") for word in words]


def count_words(text: str) -> int:
    """Number of words in ``text`` using the Unicode-aware splitter."""
    return len(split_unicode_words(text))


def tokenize_ascii(text: str) -> list[str]:
    """Lower-case ASCII alphanumeric tokens; everything else is a separator."""
    return _ASCII_TOKEN_RE.findall(text.lower())


def tokenize_unicode(text: str) -> list[str]:
    """Case-folded Unicode tokens (NFC normalised)."""
    normalised = unicodedata.normalize("NFC", text)
    return [word.casefold() for word in split_unicode_words(normalised)]


def tokenize(
    text: str,
    unicode_letters: bool = False,
    stopwords: frozenset[str] | set[str] | None = None,
    min_length: int = 2,
) -> list[str]:
    """Tokenise ``text`` and drop stopwords, short tokens and pure numbers."""
    if stopwords is None:
        stopwords = DEFAULT_STOPWORDS
    raw = tokenize_unicode(text) if unicode_letters else tokenize_ascii(text)
    tokens = []
    for token in raw:
        if len(token) < min_length or token in stopwords or token.isdigit():
            continue
        tokens.append(token)
    return tokens


SparseVector = dict[str, float]


def l2_normalize(vector: Mapping[str, float]) -> SparseVector:
    """Scale a sparse vector to unit length (zero vectors stay empty)."""
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if norm == 0:
        return {}
    return {term: value / norm for term, value in vector.items()}


def cosine_similarity(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    """Cosine similarity of two sparse vectors (not necessarily normalised)."""
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(value * b.get(term, 0.0) for term, value in a.items())
    if dot == 0:
        return 0.0
    norm_a = math.sqrt(sum(value * value for value in a.values()))
    norm_b = math.sqrt(sum(value * value for value in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class TfidfIndex:
    """TF-IDF vector space over a fixed collection of documents.

    * term frequency is sub-linear: ``1 + ln(count)``
    * inverse document frequency is smoothed: ``ln((1 + N) / (1 + df)) + 1``
    * document vectors are L2 normalised, so the dot product of two vectors is
      their cosine similarity.
    """

    def __init__(self, documents: Mapping[str, Iterable[str]] | None = None) -> None:
        self.idf: dict[str, float] = {}
        self.vectors: dict[str, SparseVector] = {}
        self.postings: dict[str, list[tuple[str, float]]] = {}
        self.document_count = 0
        if documents is not None:
            self.fit(documents)

    def fit(self, documents: Mapping[str, Iterable[str]]) -> TfidfIndex:
        counts: dict[str, Counter[str]] = {}
        document_frequency: Counter[str] = Counter()
        for doc_id, tokens in documents.items():
            counter = Counter(tokens)
            counts[doc_id] = counter
            document_frequency.update(counter.keys())
        self.document_count = len(counts)
        total = self.document_count
        self.idf = {
            term: math.log((1 + total) / (1 + frequency)) + 1.0
            for term, frequency in document_frequency.items()
        }
        self.vectors = {}
        self.postings = {}
        for doc_id, counter in counts.items():
            vector = l2_normalize(
                {
                    term: (1.0 + math.log(count)) * self.idf[term]
                    for term, count in counter.items()
                }
            )
            self.vectors[doc_id] = vector
            for term, weight in vector.items():
                self.postings.setdefault(term, []).append((doc_id, weight))
        return self

    def __len__(self) -> int:
        return self.document_count

    def __contains__(self, doc_id: object) -> bool:
        return doc_id in self.vectors

    def vector(self, doc_id: str) -> SparseVector:
        return self.vectors.get(doc_id, {})

    def transform(self, tokens: Iterable[str]) -> SparseVector:
        """Vectorise an unseen document with the fitted IDF weights."""
        counter = Counter(token for token in tokens if token in self.idf)
        return l2_normalize(
            {term: (1.0 + math.log(count)) * self.idf[term] for term, count in counter.items()}
        )

    def similarity(self, doc_a: str, doc_b: str) -> float:
        a = self.vectors.get(doc_a)
        b = self.vectors.get(doc_b)
        if not a or not b:
            return 0.0
        if len(a) > len(b):
            a, b = b, a
        return sum(weight * b.get(term, 0.0) for term, weight in a.items())

    def shared_terms(self, doc_a: str, doc_b: str, limit: int = 5) -> list[str]:
        """Terms contributing most to the similarity of two documents."""
        a = self.vectors.get(doc_a, {})
        b = self.vectors.get(doc_b, {})
        contributions = [(a[term] * b[term], term) for term in a.keys() & b.keys()]
        contributions.sort(key=lambda item: (-item[0], item[1]))
        return [term for _, term in contributions[:limit]]

    def top_terms(self, doc_id: str, limit: int = 5) -> list[str]:
        vector = self.vectors.get(doc_id, {})
        ranked = sorted(vector.items(), key=lambda item: (-item[1], item[0]))
        return [term for term, _ in ranked[:limit]]

    def most_similar(
        self,
        doc_id: str,
        candidates: set[str] | frozenset[str] | None = None,
        limit: int = 10,
        min_similarity: float = 0.0,
    ) -> list[tuple[str, float]]:
        """Documents most similar to ``doc_id`` using the inverted index."""
        return self.query(
            self.vectors.get(doc_id, {}),
            candidates=candidates,
            limit=limit,
            min_similarity=min_similarity,
            exclude={doc_id},
        )

    def query(
        self,
        vector: Mapping[str, float],
        candidates: set[str] | frozenset[str] | None = None,
        limit: int = 10,
        min_similarity: float = 0.0,
        exclude: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        scores: dict[str, float] = {}
        for term, weight in vector.items():
            for other, other_weight in self.postings.get(term, ()):
                if candidates is not None and other not in candidates:
                    continue
                scores[other] = scores.get(other, 0.0) + weight * other_weight
        if exclude:
            for doc_id in exclude:
                scores.pop(doc_id, None)
        ranked = [
            (other, score)
            for other, score in scores.items()
            if score > 0 and score >= min_similarity
        ]
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked[:limit] if limit else ranked
