# Changelog

Versions follow [semantic versioning](https://semver.org).

## 0.2.0

- Hybrid search: keywords plus embeddings (any OpenAI-compatible endpoint: Ollama, an LLM
  gateway, Azure OpenAI), merged by reciprocal rank fusion.
- The ceiling applies to the meaning side too; restricted documents are never embedded.
- One model per index; keyword fallback, reported in `search_mode`, when embeddings fail.
- `evidence-mcp eval --mode compare`, with recorded vectors for offline replay.
- A paraphrase question set that measures the vocabulary gap.
- The container's user can rebuild the index (for hybrid ingest at start-up).

## 0.1.0

- Statistics tools over SDMX, keyword document search with a sensitivity ceiling, retrieval
  evaluation as a CI gate, stdio and HTTP transports, container image.
