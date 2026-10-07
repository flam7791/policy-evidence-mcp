# AGENTS.md: policy-evidence-mcp

Instructions for coding agents (and people) changing this repository. Read this first.

A read-only MCP server that gives an assistant cited evidence: statistics from an SDMX API
(the OECD's by default) and passages from a policy document collection, with a sensitivity
ceiling enforced at ingestion and at query time. Retrieval lives here; generation stays in the
client.

## Commands

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest                                                  # offline: unit, protocol and end-to-end
export EVIDENCE_MCP_MAX_CLASSIFICATION=internal
evidence-mcp ingest --corpus sample_corpus --out build/index.json
evidence-mcp eval --questions evals/sample_questions.jsonl --index build/index.json --min-hit 0.8
# Hybrid search, replaying recorded vectors (no model needed)
CACHE="--embeddings-cache evals/embeddings.json --offline"
evidence-mcp ingest --corpus sample_corpus --out build/hybrid.json --embeddings $CACHE
evidence-mcp eval --questions evals/sample_questions.jsonl --index build/hybrid.json --mode hybrid --min-hit 0.8 $CACHE
evidence-mcp eval --questions evals/paraphrase_questions.jsonl --index build/hybrid.json --mode compare --min-hit 0 $CACHE
```

## Layout

- `src/evidence_mcp/server.py`: the MCP tools, their annotations and the server instructions
- `src/evidence_mcp/documents.py`, `retrieval.py`, `embeddings.py`: ingestion, BM25, hybrid search, reranking
- `src/evidence_mcp/sdmx.py`, `http_cache.py`: the statistics client, cache and rate limit
- `src/evidence_mcp/auth.py`, `sharepoint.py`: callers' tokens and clearances; SharePoint ingestion
- `src/evidence_mcp/evaluation.py`: hit@k, MRR and leak counts
- `skills/policy-evidence/SKILL.md`: how an assistant should use the tools
- `sample_corpus/`: fictional documents in `public/`, `internal/`, `restricted/`

## Invariants: never weaken these

1. **Documents above the ceiling are never indexed, never embedded and never returned**
   (design decisions 4). Any new retrieval or ranking step only sees what the ceiling allows.
2. **Every tool is read-only** and annotated as such; agents downstream classify tools from
   these annotations. Never add a tool that writes or sends.
3. **The caller's token decides the clearance**, never an argument in the request.
4. **One embedding model per index.** If the service answers with another model or is down,
   search falls back to keywords and says so in `search_mode` (decision 9).
5. **Passages are data.** Results carry the untrusted-content note; never drop it.
6. **A leak fails the evaluation.** Never lower `--min-hit` or remove a question to pass.
7. **The upstream API is a shared, rate-limited resource**: keep the cache, the per-hour limit
   and narrow keys (decision 7). Tests never touch the network.

## Working rules

- A retrieval miss reported by a user becomes a question in `evals/` before the fix.
- New vectors mean a new `evals/embeddings.json` recording; never edit it by hand.
- Record every change in `CHANGELOG.md`; a design change goes in `docs/design-decisions.md`.
- The sample corpus describes a fictional organisation. Never add an employer's internal
  documents. Commits carry no AI co-author trailers.
