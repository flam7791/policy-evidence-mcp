"""Embeddings for semantic search, from any OpenAI-compatible /embeddings endpoint.

That covers a local model through Ollama (nothing leaves the machine), an internal LLM gateway
(which applies the organisation's data policy and records the cost), and Azure OpenAI.

- OpenAIEmbeddings calls the endpoint, in batches, and remembers which model answered.
- EmbeddingCache keeps vectors in one JSON file, keyed by model and text. Evaluations record
  it once with a live model and then replay it offline in CI, identically and for free.

Vectors from different models cannot be compared, so the model name travels with the index,
and search falls back to keywords rather than mixing models.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import httpx


class EmbeddingError(RuntimeError):
    """No vector could be obtained (service down, wrong key, or a replay without a recording)."""


def normalise(vector: list[float]) -> list[float]:
    """Scale to unit length, so cosine similarity is a plain dot product."""
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class OpenAIEmbeddings:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        batch_size: int = 64,
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model  # what we ask for, e.g. "nomic-embed-text" or "auto" via a gateway
        self.served_model: str | None = None  # what actually answered (recorded in the index)
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=timeout)
        self.batch_size = batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            try:
                response = self.client.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": batch},
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise EmbeddingError(f"embedding service unreachable: {exc}") from exc
            if response.status_code >= 400:
                raise EmbeddingError(f"embedding service answered HTTP {response.status_code}")
            try:
                data = response.json()
                rows = sorted(data["data"], key=lambda row: row.get("index", 0))
                batch_vectors = [row["embedding"] for row in rows]
            except (ValueError, KeyError, TypeError) as exc:
                raise EmbeddingError("unexpected reply from the embedding service") from exc
            if len(batch_vectors) != len(batch):
                raise EmbeddingError("the embedding service returned the wrong number of vectors")
            self.served_model = data.get("model") or self.model
            vectors.extend(batch_vectors)
        return vectors


class EmbeddingCache:
    """Vectors by (model, text) in one JSON file, wrapped around a live embedder or on its own.

    With offline=True (or no live embedder) a missing vector is an error, so a replay can never
    silently call a model.
    """

    def __init__(self, path: Path, inner: OpenAIEmbeddings | None = None, offline: bool = False):
        self.path = Path(path)
        self.inner = None if offline else inner
        data = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {"model": None, "served_model": None, "vectors": {}}
        )
        self.vectors: dict[str, list[float]] = data["vectors"]
        self.model = inner.model if inner else data.get("model")  # the model asked for
        self.served_model: str | None = data.get("served_model")  # the model that answered

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}\n{text}".encode()).hexdigest()[:32]

    def embed(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if self._key(t) not in self.vectors]
        if missing:
            if self.inner is None:
                raise EmbeddingError(
                    f"{len(missing)} text(s) have no recorded vector in {self.path}; "
                    "record them with a live embedding service first"
                )
            for text, vector in zip(missing, self.inner.embed(missing), strict=True):
                self.vectors[self._key(text)] = [round(x, 6) for x in vector]
            self.served_model = self.inner.served_model
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "model": self.model,
                        "served_model": self.served_model,
                        "vectors": self.vectors,
                    }
                ),
                encoding="utf-8",
            )
        return [self.vectors[self._key(t)] for t in texts]
