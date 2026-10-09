# Changelog

Versions follow [semantic versioning](https://semver.org).

## 0.5.0

- `EVIDENCE_MCP_RERANK_FORMAT=list`: the reranking model answers with one grade per passage, in
  order (`{"grades": [3, 0, 1]}`), instead of a passage number and grade for each. In the first
  live run, Qwen 2.5 7B graded only 1 of 20 passages on 6 of 14 searches in the `objects`
  format; the list is shorter to produce in full. The same closed-set rules apply. The default
  stays `objects` until the list format is measured.
- The answer format is part of the grade cache: a file records its format, a replay uses it,
  and one file cannot mix formats. Recordings made with 0.4 keep their keys and keep replaying.
- First live reranking run, Qwen 2.5 7B on a laptop CPU, recorded in
  `evals/rerank-qwen2.5-7b-ctx8k.json` and replayed in CI: on the paraphrase set, hybrid search
  goes from hit@3 0.77 to 0.85 and MRR 0.77 to 0.87 with reranking, no leaks; 6 of 14 hybrid
  searches got a partial answer and kept their order; about a minute per search. Results and
  limits in the README.

## 0.4.1

- `EVIDENCE_MCP_RERANK_TIMEOUT` (default 60 seconds) sets how long a grading call may take. The
  first live run, with Qwen 2.5 7B on a laptop CPU, timed out on every question at the fixed
  60 seconds.
- A reranking service that does not answer (timeout, connection error, HTTP error) is no longer
  recorded in the grade cache, so a second run asks again instead of replaying the outage; an
  evaluation in which that happened reports INCOMPLETE and fails. Invalid answers from the model
  are still recorded and counted.

## 0.4.0

- Optional reranking: one call to an OpenAI-compatible chat model per search grades the top 20
  passages from 0 to 3, and results are re-sorted by grade. A reply that does not grade every
  passage with a valid value keeps the search order and says so in `search_mode`. The reranking
  model sees only passages the caller may see (and none above
  `EVIDENCE_MCP_RERANK_MAX_CLASSIFICATION`). Results carry `relevance`. Off by default.
- `evidence-mcp eval --rerank --rerank-cache FILE`: evaluate each mode with and without
  reranking; grades are recorded once and replayed offline. No live run is recorded yet.
- `AGENTS.md` for coding agents (`CLAUDE.md` imports it), and `skills/policy-evidence/SKILL.md`
  for assistants that call the tools.

## 0.3.1

- Microsoft 365 Copilot through Copilot Studio: custom connector (streamable MCP, OAuth 2.0 with
  Entra ID), agent instructions and setup steps in `integrations/copilot-studio`, with tests.
- Hosts are accepted without a port, as sent through a TLS reverse proxy or ingress (they were
  refused with DNS-rebinding protection on).

## 0.3.0

- Access management on the HTTP transport: bearer tokens issued with `evidence-mcp token create`
  (stored as hashes), or Microsoft Entra ID access tokens validated against the tenant's keys,
  with clearance from app roles. Each caller sees documents up to the lower of the server's
  ceiling and their clearance. Serving on a network address without authentication is refused
  unless `--allow-unauthenticated` is given. The container now requires a token file.
- `evidence-mcp sync-sharepoint`: a SharePoint library through Microsoft Graph (Sites.Selected),
  classification from Purview sensitivity labels, unlabelled files treated as restricted,
  incremental by eTag, citations pointing to SharePoint. Word documents (.docx) are indexed.
- OpenTelemetry tracing (optional `tracing` extra): the access decision on each tool-call span,
  no query or passage text.

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
