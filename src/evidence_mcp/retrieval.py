"""Retrieval over document chunks, with a sensitivity ceiling: keywords (BM25), and optionally
hybrid search that adds meaning (embeddings) and fuses both rankings.

Why BM25 and not embeddings for v1:
- no model, no API key, no per-query cost, and fully deterministic, so the evaluation in CI
  gives the same number every time;
- strong on the exact terms policy questions use (names of rules, labels, record types);
- weak on paraphrases ("staff" vs "employees"). The evaluation set measures that gap, and the
  retriever is one class, so a hybrid (BM25 + embeddings) version can replace it later.

BM25 in one line: a chunk scores highly when it contains the query's rarer words (IDF) several
times (TF), with diminishing returns (k1) and a penalty for very long chunks (b).

Hybrid search (version 0.2) closes the paraphrase gap. At ingest, each chunk also gets an
embedding vector. At query time, the keyword ranking and the meaning ranking (cosine similarity)
are combined by reciprocal rank fusion: each chunk scores the sum of 1 / (60 + rank) over both
lists. Fusion needs no tuning of score scales, and a chunk found by both methods rises to the
top. If the embedding service is down, or answers with a different model than the one the index
was built with, search falls back to keywords and says so.
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import classification_allowed
from .documents import DEFAULT_MAX_CHARS, Chunk, chunk_document, iter_corpus
from .embeddings import EmbeddingError, dot, normalise

log = logging.getLogger(__name__)
RRF_K = 60  # the usual constant for reciprocal rank fusion

INDEX_FORMAT_VERSION = 1

# Common English words that carry no meaning for retrieval.
_STOPWORD_TEXT = """
a an and are as at be been but by can could do does for from had has have how i if in into is
it its may might must not of on or our shall should so such that the their them then there
these they this those to was we were what when where which who whom why will with would you your
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))  # strip accents
    return [t for t in re.findall(r"[a-z0-9]+", text) if len(t) > 1 and t not in STOPWORDS]


@dataclass
class SearchHit:
    score: float
    chunk: Chunk
    matched_by: str = "keywords"  # "keywords", "meaning" or "keywords and meaning"


class Bm25Index:
    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.term_freqs = [
            Counter(tokenize(c.title + " " + (c.section or "") + " " + c.text)) for c in chunks
        ]
        self.lengths = [sum(tf.values()) for tf in self.term_freqs]
        self.avg_length = (sum(self.lengths) / len(self.lengths)) if chunks else 0.0
        doc_freq: Counter[str] = Counter()
        for tf in self.term_freqs:
            doc_freq.update(tf.keys())
        n = len(chunks)
        # Lucene-style IDF: always positive, larger for rarer terms.
        self.idf = {
            term: math.log(1 + (n - df + 0.5) / (df + 0.5)) for term, df in doc_freq.items()
        }

    def search(self, query: str, top_k: int, ceiling: str) -> list[SearchHit]:
        terms = set(tokenize(query))
        hits = []
        for i, chunk in enumerate(self.chunks):
            # Defence in depth: the index should not contain documents above the ceiling,
            # but the filter is applied again at query time in case the index was built
            # with a higher clearance than the server is running with.
            if not classification_allowed(chunk.classification, ceiling):
                continue
            tf = self.term_freqs[i]
            norm = self.k1 * (1 - self.b + self.b * self.lengths[i] / (self.avg_length or 1))
            score = sum(
                self.idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in terms if t in tf
            )
            if score > 0:
                hits.append(SearchHit(score=score, chunk=chunk))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]


def embedding_text(chunk: Chunk) -> str:
    return f"{chunk.title}\n{chunk.section or ''}\n{chunk.text}"


class HybridIndex:
    """Keywords and meaning, fused. Behaves like Bm25Index, and reports which mode it used."""

    def __init__(
        self, bm25: Bm25Index, vectors: list[list[float]], model: str, embedder, depth: int = 50
    ):
        if len(vectors) != len(bm25.chunks):
            raise ValueError("the index has a different number of vectors and chunks")
        self.bm25 = bm25
        self.chunks = bm25.chunks
        self.vectors = vectors
        self.model = model  # the model that embedded the chunks
        self.embedder = embedder
        self.depth = depth
        self.last_mode = "hybrid"
        self.fallbacks = 0  # queries answered with keywords only

    def _query_vector(self, query: str) -> list[float] | None:
        try:
            [vector] = self.embedder.embed([query])
        except EmbeddingError as exc:
            log.warning("semantic search unavailable, using keywords only: %s", exc)
            self.last_mode = "keywords only (embedding service unavailable)"
            self.fallbacks += 1
            return None
        served = getattr(self.embedder, "served_model", None) or self.embedder.model
        if served != self.model:
            log.warning("index built with %s but %s answered; keywords only", self.model, served)
            self.last_mode = f"keywords only (index built with {self.model}, not {served})"
            self.fallbacks += 1
            return None
        self.last_mode = "hybrid"
        return normalise(vector)

    def search(self, query: str, top_k: int, ceiling: str) -> list[SearchHit]:
        keyword_hits = self.bm25.search(query, self.depth, ceiling)
        query_vector = self._query_vector(query)
        if query_vector is None:
            return keyword_hits[:top_k]

        allowed = [
            i
            for i, chunk in enumerate(self.chunks)
            if classification_allowed(chunk.classification, ceiling)  # same ceiling, both sides
        ]
        similarity = sorted(
            ((dot(query_vector, self.vectors[i]), i) for i in allowed), reverse=True
        )[: self.depth]

        position = {chunk.chunk_id: i for i, chunk in enumerate(self.chunks)}
        fused: dict[int, float] = {}
        found_by: dict[int, set[str]] = {}
        for rank, hit in enumerate(keyword_hits, start=1):
            i = position[hit.chunk.chunk_id]
            fused[i] = fused.get(i, 0.0) + 1 / (RRF_K + rank)
            found_by.setdefault(i, set()).add("keywords")
        for rank, (_, i) in enumerate(similarity, start=1):
            fused[i] = fused.get(i, 0.0) + 1 / (RRF_K + rank)
            found_by.setdefault(i, set()).add("meaning")

        ranked = sorted(fused.items(), key=lambda item: item[1], reverse=True)[:top_k]
        return [
            SearchHit(
                score=score * 100,  # readable numbers; only the order matters
                chunk=self.chunks[i],
                matched_by=" and ".join(sorted(found_by[i])),
            )
            for i, score in ranked
        ]


# --------------------------------------------------------------------------- index files


def build_index(
    corpus_dir: Path, ceiling: str, max_chars: int = DEFAULT_MAX_CHARS, embedder=None
) -> dict:
    """Read the corpus and return a JSON-serialisable index (with vectors if an embedder is
    given).

    Documents above the ceiling are never read into the index, so they cannot leak later, and
    they are never sent to an embedding service either.
    """
    documents, chunks, excluded = [], [], 0
    for doc in iter_corpus(corpus_dir):
        if not classification_allowed(doc.classification, ceiling):
            excluded += 1  # count only: even a file name can be sensitive
            continue
        doc_chunks = chunk_document(doc, max_chars)
        documents.append(
            {
                "doc_id": doc.doc_id,
                "title": doc.title,
                "source": doc.source,
                "classification": doc.classification,
                "path": doc.path,
                "chunks": len(doc_chunks),
            }
        )
        chunks.extend(c.to_dict() for c in doc_chunks)
    index = {
        "format_version": INDEX_FORMAT_VERSION,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ceiling": ceiling,
        "documents": documents,
        "excluded_documents": excluded,
        "chunks": chunks,
    }
    if embedder is not None and chunks:
        vectors = embedder.embed([embedding_text(Chunk(**c)) for c in chunks])
        index["embedding_model"] = getattr(embedder, "served_model", None) or embedder.model
        index["vectors"] = [[round(x, 6) for x in normalise(v)] for v in vectors]
    return index


def save_index(index: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")


def load_index(path: Path) -> tuple[dict, Bm25Index]:
    index = json.loads(path.read_text(encoding="utf-8"))
    if index.get("format_version") != INDEX_FORMAT_VERSION:
        raise ValueError(f"{path} was built by another version; run `evidence-mcp ingest` again.")
    chunks = [Chunk(**c) for c in index["chunks"]]
    return index, Bm25Index(chunks)


def searcher(index: dict, bm25: Bm25Index, embedder=None) -> Bm25Index | HybridIndex:
    """Hybrid search when the index has vectors and an embedder is configured; else keywords."""
    if embedder is not None and index.get("vectors"):
        return HybridIndex(bm25, index["vectors"], index["embedding_model"], embedder)
    return bm25
