"""Hybrid search (0.2): vectors at ingest, rank fusion, the same ceiling on both sides, safe
fallbacks, recorded embeddings, and the embeddings client."""

import json
import re
import zlib

import httpx
import pytest
from mcp import Client

from evidence_mcp.cli import main
from evidence_mcp.documents import Chunk
from evidence_mcp.embeddings import EmbeddingCache, EmbeddingError, OpenAIEmbeddings
from evidence_mcp.evaluation import evaluate, load_questions
from evidence_mcp.retrieval import Bm25Index, HybridIndex, build_index, save_index, searcher
from evidence_mcp.server import create_server

from .conftest import ROOT, SAMPLE_CORPUS
from .test_server import payload

# A toy "meaning" model for tests: words are mapped to shared concepts, then hashed into a small
# vector. It knows that a chatbot is AI and that applicants are about recruitment, which is
# exactly what keyword search cannot know. Real runs use a real embedding model.
CONCEPTS = {
    "chatbot": "ai", "bot": "ai", "machine": "ai", "generative": "ai", "tools": "ai",
    "hire": "recruitment", "applicants": "recruitment", "job": "recruitment",
    "recruitment": "recruitment", "promotion": "recruitment", "individuals": "recruitment",
    "decide": "decision", "pick": "decision", "decisions": "decision", "rely": "decision",
}  # fmt: skip


class ConceptEmbedder:
    def __init__(self, model="concept-v1", fail=False):
        self.model = model
        self.served_model = model
        self.fail = fail
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise EmbeddingError("service down")
        vectors = []
        for text in texts:
            vector = [0.0] * 64
            for word in re.findall(r"[a-z]+", text.lower()):
                concept = CONCEPTS.get(word)
                if concept:
                    vector[zlib.crc32(concept.encode()) % 64] += 1.0
            vectors.append(vector)
        return vectors


def hybrid(ceiling="internal", embedder=None):
    embedder = embedder or ConceptEmbedder()
    index = build_index(SAMPLE_CORPUS, ceiling, embedder=embedder)
    bm25 = Bm25Index([Chunk(**c) for c in index["chunks"]])
    return index, searcher(index, bm25, embedder)


def test_ingest_stores_unit_vectors_and_the_model_that_made_them():
    index, search = hybrid()
    assert index["embedding_model"] == "concept-v1"
    assert len(index["vectors"]) == len(index["chunks"])
    assert isinstance(search, HybridIndex)


def test_restricted_documents_are_never_sent_for_embedding():
    seen = []

    class Recorder(ConceptEmbedder):
        def embed(self, texts):
            seen.extend(texts)
            return super().embed(texts)

    build_index(SAMPLE_CORPUS, "internal", embedder=Recorder())
    assert seen and not any("budget scenario" in t.lower() for t in seen)


def test_meaning_rescues_a_paraphrase_that_keywords_miss():
    question = "Can employees rely on a chatbot to pick which job applicants to hire?"
    _, search = hybrid()
    keywords_only = [h.chunk.doc_id for h in search.bm25.search(question, 3, "internal")]
    fused = search.search(question, 3, "internal")
    assert "public/ai-use-policy" not in keywords_only
    assert fused[0].chunk.doc_id == "public/ai-use-policy"
    assert fused[0].matched_by == "meaning" and search.last_mode == "hybrid"


def test_the_ceiling_applies_to_meaning_as_well_as_keywords():
    # An index that wrongly holds a restricted document, searched with a lower clearance.
    _, search = hybrid(ceiling="restricted")
    hits = search.search("What budget cuts are planned for the Board?", 10, "internal")
    assert hits and all(h.chunk.classification != "restricted" for h in hits)


def test_a_failing_embedding_service_falls_back_to_keywords_and_says_so():
    embedder = ConceptEmbedder()
    _, search = hybrid(embedder=embedder)
    embedder.fail = True
    hits = search.search("email retention", 3, "internal")
    assert hits and all(h.matched_by == "keywords" for h in hits)
    assert "unavailable" in search.last_mode and search.fallbacks == 1


def test_vectors_from_another_model_are_never_mixed():
    _, search = hybrid()
    search.embedder = ConceptEmbedder(model="another-model")
    search.search("email retention", 3, "internal")
    assert "not another-model" in search.last_mode


def test_hybrid_evaluation_has_no_leaks_on_either_question_set():
    _, search = hybrid()
    for name in ("sample_questions.jsonl", "paraphrase_questions.jsonl"):
        report = evaluate(search, load_questions(ROOT / "evals" / name), "internal", 3)
        assert report.leaks == 0


def test_openai_embeddings_client_batches_and_keeps_order():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append((request.headers.get("authorization"), body))
        data = [{"index": i, "embedding": [float(len(t))]} for i, t in enumerate(body["input"])][
            ::-1
        ]  # out of order on purpose
        return httpx.Response(200, json={"data": data, "model": "served-model"})

    client = OpenAIEmbeddings(
        "http://gw/v1",
        "auto",
        api_key="gw-key",
        batch_size=2,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.embed(["a", "bb", "ccc"]) == [[1.0], [2.0], [3.0]]
    assert [len(body["input"]) for _, body in requests] == [2, 1]
    assert requests[0][0] == "Bearer gw-key" and client.served_model == "served-model"


def test_embedding_errors_are_clear():
    down = OpenAIEmbeddings(
        "http://gw/v1", "m", client=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(503)))
    )  # fmt: skip
    with pytest.raises(EmbeddingError, match="503"):
        down.embed(["x"])


def test_recorded_vectors_replay_offline_and_never_call_a_model(tmp_path):
    cache_file = tmp_path / "vectors.json"
    live = ConceptEmbedder()
    first = EmbeddingCache(cache_file, live).embed(["hire applicants", "records"])
    replay = EmbeddingCache(cache_file, offline=True)
    assert replay.embed(["records", "hire applicants"]) == [first[1], first[0]]
    assert replay.served_model == "concept-v1"
    with pytest.raises(EmbeddingError, match="no recorded vector"):
        replay.embed(["something new"])


def test_cli_compares_keywords_and_hybrid_from_recorded_vectors(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("EVIDENCE_MCP_MAX_CLASSIFICATION", "internal")
    cache_file = tmp_path / "vectors.json"
    questions = ROOT / "evals" / "sample_questions.jsonl"
    # Record: build the index and embed every question once through the live (toy) model.
    live_cache = EmbeddingCache(cache_file, ConceptEmbedder())
    index = build_index(SAMPLE_CORPUS, "internal", embedder=live_cache)
    live_cache.embed([q["question"] for q in load_questions(questions)])
    save_index(index, tmp_path / "index.json")
    # Replay offline through the command line.
    code = main(
        [
            "eval",
            "--questions",
            str(questions),
            "--index",
            str(tmp_path / "index.json"),
            "--mode",
            "compare",
            "--embeddings-cache",
            str(cache_file),
            "--offline",
        ]
    )
    out = capsys.readouterr().out
    assert "| keywords |" in out and "| hybrid |" in out
    assert code == 0


async def test_the_server_reports_its_search_mode(settings, fetcher):
    embedder = ConceptEmbedder()
    save_index(build_index(SAMPLE_CORPUS, "internal", embedder=embedder), settings.index_path)
    server = create_server(settings, fetcher, embedder=embedder)
    async with Client(server) as client:
        listing = payload(await client.call_tool("list_documents", {}))
        found = payload(
            await client.call_tool(
                "search_documents", {"query": "Can a chatbot pick job applicants?"}
            )
        )
    assert listing["search"] == "hybrid" and listing["embedding_model"] == "concept-v1"
    assert found["search_mode"] == "hybrid"
    assert found["results"][0]["matched_by"] in ("meaning", "keywords and meaning")
