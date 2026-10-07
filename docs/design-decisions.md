# Design decisions

Short records of the choices that shape this project: the context, the decision, and what it
costs. Each one can be revisited when the context changes.

## 1. MCP server rather than a chatbot

**Context.** Organisations already have assistants (Copilot, ChatGPT, Claude). What they lack is
governed access from those assistants to trusted sources.
**Decision.** Build a Model Context Protocol server: tools any MCP client can call.
**Consequences.** One integration serves every compatible assistant, and the server never holds a
model or an API key. The server cannot control how the client model uses the evidence, so tool
descriptions, citations and the `evidence_brief` prompt carry that guidance.

## 2. Retrieval in the server, generation in the client

**Context.** A server that also generated answers would need a model, keys, a budget and its own
evaluation of answer quality.
**Decision.** The server returns passages and data with citations; the client writes the answer.
**Consequences.** Zero server-side token cost and a clean evaluation target (did retrieval find
the right document?). Answer quality still depends on the client model.

## 3. BM25 first, embeddings later

**Context.** Embeddings handle paraphrase better but add a model, cost and non-determinism.
**Decision.** Ship keyword retrieval (BM25) behind a single class, with an evaluation set in CI.
**Consequences.** Deterministic, free and fast; weak on vocabulary mismatch, which the evaluation
shows explicitly (question p01). A hybrid retriever can replace the class and be judged on the
same questions.

## 4. Sensitivity ceiling at ingestion and at query time

**Context.** Policy collections mix public, internal and restricted material. A retrieval system
that indexes everything and filters later can leak through a bug, a misconfiguration or a log.
**Decision.** Documents above the ceiling are never read into the index; results are filtered
again at query time; excluded documents are counted, not named; unknown labels count as most
sensitive. Leak checks are part of the evaluation and fail CI.
**Consequences.** Safe by default (`public`). One ceiling per server instance, not per user: fine
for a team tool, not for an enterprise deployment with mixed permissions (roadmap).

## 5. Classification from folders, overridable by front matter

**Context.** Document owners already organise files in libraries by audience.
**Decision.** A file under `internal/` is internal unless its front matter says otherwise; files
outside any level folder are public.
**Consequences.** Labelling costs nothing for existing collections. The weakness is the default:
an unlabelled sensitive file placed at the root would be treated as public, so the ceiling
default is `public` and the ingest command reports how many documents it skipped.

## 6. SDMX, parsed by element names, with CSV for data

**Context.** SDMX is the ISO standard used by the OECD, ECB, Eurostat and IMF. Its XML uses
namespaces that differ slightly across providers and versions.
**Decision.** Parse structures by local element names, ignore namespaces, and read only direct
children of the dimension list; request data as CSV with labels.
**Consequences.** Works across providers with one small parser. CSV is compact and readable by
a model; labels make codes self-explanatory without an extra lookup.

## 7. Treat the public API as a shared, rate-limited resource

**Context.** The OECD API allows about 60 requests per hour per client and blocks clients that
exceed it. An assistant in a loop can exhaust that quickly.
**Decision.** Disk cache with per-type lifetimes, a persisted sliding-window limit set below the
provider's, brief retries on 429/5xx, and hard response-size caps.
**Consequences.** Predictable load on the provider and fast repeat answers. Cached data can be up
to an hour old, which is acceptable for official statistics that change on release cycles.

## 8. Tests without the network

**Context.** Tests that call a live API are slow, flaky and would spend the rate limit.
**Decision.** Recorded fixtures behind an `httpx.MockTransport`; protocol tests through the SDK's
in-process client; one end-to-end test that starts the real server over stdio; a separate manual
`scripts/smoke_live.py` for the live service.
**Consequences.** The suite runs in about two seconds, anywhere. The fixtures can drift from the
live format, which is what the smoke script is for.

## 9. Hybrid search by rank fusion, one model per index

**Context.** Keyword search misses questions worded unlike the documents: on the paraphrase set,
it finds the right document in the top 3 only about half the time.
**Decision.** Add embeddings from any OpenAI-compatible endpoint and merge the keyword and
similarity rankings by reciprocal rank fusion, which needs no calibration between the two score
scales. The index records the embedding model; a query answered by another model, or no model,
falls back to keywords and says so.
**Consequences.** Better recall on paraphrases where the evaluation shows it, with no new
failure mode: an embedding outage degrades search instead of breaking it. Changing the
embedding model means re-indexing, deliberately.

## 10. Reranking as a bounded judgment, off by default

**Context.** After hybrid search, three paraphrases still miss, for example *"Which committee
signs off on AI projects that affect people?"*, whose answer says "Digital Governance Board".
Ranking by words and by meaning does not read a passage as an answer to a question.
**Decision.** An optional reranker: one call to an OpenAI-compatible chat model per query, which
grades each of the top 20 passages from 0 to 3. The reply must grade every passage with a valid
value; anything else keeps the search order and says so in `search_mode`. Grades reorder, they
never add a passage. The reranking model sees only what the caller may see, and an optional
lower ceiling keeps sensitive passages out of its prompt. Grades are recorded and replayed like
the vectors. A cross-encoder was the alternative: cheaper per query, but one more model type to
host, and no local option as simple as the Ollama endpoint the platform already runs.
**Consequences.** A model call per search, so latency and, with an external model, cost and data
egress: off unless `EVIDENCE_MCP_RERANK_URL` is set, and in the platform it goes through the
gateway with a `local_only` team. Whether it pays is measured on the paraphrase set with
`--rerank`; until that run is recorded, the README reports no result for it.
