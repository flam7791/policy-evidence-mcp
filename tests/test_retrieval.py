import json

import pytest

from evidence_mcp.documents import Chunk
from evidence_mcp.retrieval import Bm25Index, build_index, load_index, save_index, tokenize

from .conftest import SAMPLE_CORPUS


def chunk(cid, text, classification="public"):
    return Chunk(cid, cid, "T", None, None, text, classification, "src")


def test_tokenize_lowercases_strips_accents_and_stopwords():
    assert tokenize("The États-Unis and R&D") == ["etats", "unis"]


def test_rarer_matching_term_ranks_higher():
    index = Bm25Index(
        [
            chunk("a", "retention period for contracts"),
            chunk("b", "retention of email"),
            chunk("c", "retention of staff files"),
        ]
    )
    hits = index.search("contracts retention", top_k=3, ceiling="public")
    assert hits[0].chunk.chunk_id == "a"


def test_no_match_returns_nothing():
    index = Bm25Index([chunk("a", "retention period")])
    assert index.search("budget", top_k=3, ceiling="public") == []


def test_query_time_ceiling_filters_chunks_even_if_indexed():
    index = Bm25Index([chunk("a", "budget scenarios", "restricted"), chunk("b", "budget rules")])
    ids = [h.chunk.chunk_id for h in index.search("budget", 5, ceiling="internal")]
    assert ids == ["b"]


def test_build_index_excludes_documents_above_ceiling_and_hides_their_names():
    index = build_index(SAMPLE_CORPUS, ceiling="internal")
    doc_ids = {d["doc_id"] for d in index["documents"]}
    assert "restricted/board-budget-scenarios" not in doc_ids
    assert index["excluded_documents"] == 1
    assert "budget-scenarios" not in json.dumps(index)  # not even the file name leaks


def test_default_public_ceiling_indexes_only_public_documents():
    index = build_index(SAMPLE_CORPUS, ceiling="public")
    assert {d["classification"] for d in index["documents"]} == {"public"}


def test_index_round_trip(tmp_path):
    path = tmp_path / "index.json"
    save_index(build_index(SAMPLE_CORPUS, ceiling="internal"), path)
    meta, index = load_index(path)
    assert len(index.chunks) == len(meta["chunks"]) > 0


def test_index_from_another_version_is_rejected(tmp_path):
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"format_version": 999, "chunks": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_index(path)
