# System card: policy-evidence-mcp

| | |
|---|---|
| Pattern | P1 governed retrieval and P2 read-only MCP tool server ([ai-engineering-framework](https://github.com/flam7791/ai-engineering-framework)) |
| Models | None for keyword search; an embedding model for hybrid search (local `nomic-embed-text` through Ollama by default, or any OpenAI-compatible endpoint) |
| Classification ceiling | Set per server (`EVIDENCE_MCP_MAX_CLASSIFICATION`) |

## Intended use

Give AI assistants and agents cited, read-only access to official statistics (any SDMX 2.1 API)
and to an organisation's policy documents, so their answers can point to a source.

## Out of scope

Writing, sending or deleting anything; per-user permissions (one ceiling per server instance);
interpreting statistics (the server returns data with provenance, the client interprets).

## Data

Statistics come from the configured public API with their query URL. Documents are indexed
locally; documents above the ceiling are skipped at ingestion and filtered again at query time.
The index records the number of excluded documents, not their names. With local embeddings, no
document text leaves the machine.

## How it can fail

- A relevant passage is missed (keyword search is weak on paraphrases; hybrid search narrows the
  gap but does not close it, see the evaluation in the README).
- Indirect prompt injection in a document reaches the client as data; results are marked as data,
  a mitigation and not a guarantee. The client must still treat them as untrusted.
- The HTTP transport has no authentication of its own.

## Evaluation

Retrieval quality gate in CI (hit@3 at or above 0.8) and a leak count that must be zero, for
keyword and hybrid modes, on direct and paraphrased questions. Results and their limits are in
the README.

## Human oversight

The server answers tool calls; the people reading the assistant's answer see the citations and
the query URLs and can check them.
