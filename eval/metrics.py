"""Retrieval and answer metrics over golden-set rows. Pure: no I/O, no models.

- hit@5: an expected (document, page) lies inside the page range of one of the top 5 blocks.
- MRR: 1 / rank of the first such block among all returned blocks (0 if none).
- recall@5: share of a row's expected pages found in the top 5 (reported for `multi` rows).
- faithfulness: supported sentences / judged sentences over every non-abstained answer (micro).
- abstention accuracy: share of all rows where abstained == (not answerable).
- false-abstain rate: share of answerable rows that abstained.

A row that errored counts as a miss everywhere: hit 0, RR 0, abstention wrong, and its judged
sentences unsupported. Errors are never dropped from a denominator.
"""

from collections import defaultdict
from dataclasses import dataclass, field

K = 5


@dataclass(frozen=True)
class Expected:
    document: str
    page: int


@dataclass(frozen=True)
class RankedBlock:
    document: str
    page_from: int
    page_to: int

    def covers(self, exp: Expected) -> bool:
        return self.document == exp.document and self.page_from <= exp.page <= self.page_to


@dataclass
class RowResult:
    id: str
    kind: str
    answerable: bool
    expected: list[Expected]
    blocks: list[RankedBlock] = field(default_factory=list)
    abstained: bool | None = None  # None: no answer (error)
    sentences_total: int = 0  # judged sentences (0 when abstained)
    sentences_supported: int = 0
    tokens: int | None = None  # retrieval + answer calls; None when the row errored
    latency_ms: float | None = None
    error: str | None = None


def hit_at_k(blocks: list[RankedBlock], expected: list[Expected], k: int = K) -> bool:
    return any(b.covers(e) for b in blocks[:k] for e in expected)


def reciprocal_rank(blocks: list[RankedBlock], expected: list[Expected]) -> float:
    for rank, block in enumerate(blocks, start=1):
        if any(block.covers(e) for e in expected):
            return 1.0 / rank
    return 0.0


def recall_at_k(blocks: list[RankedBlock], expected: list[Expected], k: int = K) -> float:
    if not expected:
        return 0.0
    return sum(any(b.covers(e) for b in blocks[:k]) for e in expected) / len(expected)


def abstention_correct(row: RowResult) -> bool:
    return row.abstained is not None and row.abstained == (not row.answerable)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


@dataclass(frozen=True)
class Summary:
    n_questions: int
    n_answerable: int
    hit_at_5: float | None
    mrr: float | None
    recall_at_5_multi: float | None
    faithfulness: float | None
    sentences_judged: int
    abstention_accuracy: float | None
    false_abstain_rate: float | None
    unanswerable_abstained: float | None
    avg_tokens: float | None
    avg_latency_ms: float | None
    n_errors: int
    hit_at_5_by_kind: dict[str, float]


def summarise(rows: list[RowResult]) -> Summary:
    answerable = [r for r in rows if r.answerable]
    unanswerable = [r for r in rows if not r.answerable]
    multi = [r for r in answerable if r.kind == "multi"]

    by_kind: dict[str, list[float]] = defaultdict(list)
    for r in answerable:
        by_kind[r.kind].append(float(hit_at_k(r.blocks, r.expected)))

    judged = sum(r.sentences_total for r in rows)
    supported = sum(r.sentences_supported for r in rows)
    measured = [r for r in rows if r.error is None]

    return Summary(
        n_questions=len(rows),
        n_answerable=len(answerable),
        hit_at_5=_mean([float(hit_at_k(r.blocks, r.expected)) for r in answerable]),
        mrr=_mean([reciprocal_rank(r.blocks, r.expected) for r in answerable]),
        recall_at_5_multi=_mean([recall_at_k(r.blocks, r.expected) for r in multi]),
        faithfulness=supported / judged if judged else None,
        sentences_judged=judged,
        abstention_accuracy=_mean([float(abstention_correct(r)) for r in rows]),
        false_abstain_rate=_mean([float(r.abstained is True) for r in answerable]),
        unanswerable_abstained=_mean([float(r.abstained is True) for r in unanswerable]),
        avg_tokens=_mean([float(r.tokens) for r in measured if r.tokens is not None]),
        avg_latency_ms=_mean([r.latency_ms for r in measured if r.latency_ms is not None]),
        n_errors=sum(r.error is not None for r in rows),
        hit_at_5_by_kind={kind: sum(v) / len(v) for kind, v in sorted(by_kind.items())},
    )
