"""Keyword retrieval (BM25) over document chunks, with a sensitivity ceiling.

Why BM25 and not embeddings for v1:
- no model, no API key, no per-query cost, and fully deterministic, so the evaluation in CI
  gives the same number every time;
- strong on the exact terms policy questions use (names of rules, labels, record types);
- weak on paraphrases ("staff" vs "employees"). The evaluation set measures that gap, and the
  retriever is one class, so a hybrid (BM25 + embeddings) version can replace it later.

BM25 in one line: a chunk scores highly when it contains the query's rarer words (IDF) several
times (TF), with diminishing returns (k1) and a penalty for very long chunks (b).
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import classification_allowed
from .documents import DEFAULT_MAX_CHARS, Chunk, chunk_document, iter_corpus

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


# --------------------------------------------------------------------------- index files


def build_index(corpus_dir: Path, ceiling: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """Read the corpus and return a JSON-serialisable index.

    Documents above the ceiling are never read into the index, so they cannot leak later.
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
    return {
        "format_version": INDEX_FORMAT_VERSION,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ceiling": ceiling,
        "documents": documents,
        "excluded_documents": excluded,
        "chunks": chunks,
    }


def save_index(index: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")


def load_index(path: Path) -> tuple[dict, Bm25Index]:
    index = json.loads(path.read_text(encoding="utf-8"))
    if index.get("format_version") != INDEX_FORMAT_VERSION:
        raise ValueError(f"{path} was built by another version; run `evidence-mcp ingest` again.")
    chunks = [Chunk(**c) for c in index["chunks"]]
    return index, Bm25Index(chunks)
