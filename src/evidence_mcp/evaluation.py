"""Retrieval evaluation: does search put the right document near the top?

Question file format (JSON Lines, one object per line):
    {"id": "q01", "question": "...", "expected_doc": "public/ai-use-policy"}
    {"id": "s01", "question": "...", "forbidden_doc": "restricted/board-budget-scenarios"}

Metrics:
- hit@1 and hit@k: share of questions whose expected document appears at rank 1 / in the top k
- MRR (mean reciprocal rank): 1 for rank 1, 0.5 for rank 2, ... 0 when absent; rewards
  putting the right answer first rather than merely somewhere in the list
- leaks: number of "forbidden_doc" questions where the forbidden document was returned at all.
  Any leak fails the evaluation, whatever the other scores.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .retrieval import Bm25Index


@dataclass
class EvalReport:
    k: int
    questions: int = 0
    hit_at_1: float = 0.0
    hit_at_k: float = 0.0
    mrr: float = 0.0
    leaks: int = 0
    misses: list[dict] = field(default_factory=list)

    def passed(self, min_hit_at_k: float) -> bool:
        return self.leaks == 0 and self.hit_at_k >= min_hit_at_k

    def summary(self) -> str:
        return (
            f"questions={self.questions} hit@1={self.hit_at_1:.2f} "
            f"hit@{self.k}={self.hit_at_k:.2f} MRR={self.mrr:.2f} leaks={self.leaks}"
        )


def load_questions(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip() and not line.startswith("//")]


def evaluate(index: Bm25Index, questions: list[dict], ceiling: str, k: int = 3) -> EvalReport:
    report = EvalReport(k=k)
    reciprocal_ranks, hits1, hitsk = [], 0, 0
    depth = max(k, 10)  # look deeper than k so MRR can credit ranks 4..10

    for q in questions:
        results = [hit.chunk.doc_id for hit in index.search(q["question"], depth, ceiling)]

        if "forbidden_doc" in q:
            if q["forbidden_doc"] in results:
                report.leaks += 1
                report.misses.append({"id": q["id"], "problem": "forbidden document returned"})
            continue

        expected = q["expected_doc"]
        rank = results.index(expected) + 1 if expected in results else None
        reciprocal_ranks.append(1 / rank if rank else 0.0)
        hits1 += rank == 1
        hitsk += bool(rank and rank <= k)
        if not rank or rank > k:
            report.misses.append(
                {"id": q["id"], "question": q["question"], "expected": expected, "got": results[:k]}
            )

    scored = len(reciprocal_ranks)
    report.questions = len(questions)
    if scored:
        report.hit_at_1 = hits1 / scored
        report.hit_at_k = hitsk / scored
        report.mrr = sum(reciprocal_ranks) / scored
    return report
