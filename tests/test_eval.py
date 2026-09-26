"""The retrieval evaluation runs in CI like any other test: quality is a release gate."""

from evidence_mcp.documents import Chunk
from evidence_mcp.evaluation import evaluate, load_questions
from evidence_mcp.retrieval import Bm25Index, build_index

from .conftest import ROOT, SAMPLE_CORPUS

MIN_HIT_AT_3 = 0.8


def test_sample_evaluation_meets_threshold_with_zero_leaks():
    index = build_index(SAMPLE_CORPUS, ceiling="internal")
    bm25 = Bm25Index([Chunk(**c) for c in index["chunks"]])
    questions = load_questions(ROOT / "evals" / "sample_questions.jsonl")

    report = evaluate(bm25, questions, ceiling="internal", k=3)

    assert report.leaks == 0, report.misses
    assert report.hit_at_k >= MIN_HIT_AT_3, report.summary()


def test_a_leak_fails_the_evaluation_whatever_the_scores():
    # Build an index that wrongly contains the restricted document, and search with a
    # ceiling that lets it through: the forbidden-document questions must catch it.
    index = build_index(SAMPLE_CORPUS, ceiling="restricted")
    bm25 = Bm25Index([Chunk(**c) for c in index["chunks"]])
    questions = load_questions(ROOT / "evals" / "sample_questions.jsonl")

    report = evaluate(bm25, questions, ceiling="restricted", k=3)

    assert report.leaks > 0
    assert not report.passed(MIN_HIT_AT_3)
