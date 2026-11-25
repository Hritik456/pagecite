"""eval/golden.jsonl: loading and schema rules (shared by make_review.py, run.py and the unit test).

Row: {id, question, expected: [{document, page, quote}], reference_answer, answerable, kind}
- P5.md's schema plus `quote` (the exact source sentence, for review and the overlap score) and
  `kind` (for per-kind breakdowns).
- answerable rows have >= 1 expected entry; unanswerable rows have none.
"""

import json
from dataclasses import dataclass
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent / "golden.jsonl"
KINDS = ("semantic", "identifier", "bank_type", "multi", "unanswerable")
MIN_ROWS, MAX_ROWS = 30, 50


@dataclass(frozen=True)
class ExpectedPage:
    document: str
    page: int
    quote: str


@dataclass(frozen=True)
class GoldenRow:
    id: str
    question: str
    expected: list[ExpectedPage]
    reference_answer: str
    answerable: bool
    kind: str


class GoldenError(ValueError):
    pass


def parse_row(obj: dict, line_no: int) -> GoldenRow:
    where = f"golden.jsonl line {line_no}"
    keys = {"id", "question", "expected", "reference_answer", "answerable", "kind"}
    if not isinstance(obj, dict) or set(obj) != keys:
        raise GoldenError(f"{where}: keys must be exactly {sorted(keys)}")
    if obj["kind"] not in KINDS:
        raise GoldenError(f"{where}: unknown kind {obj['kind']!r}")
    if not isinstance(obj["answerable"], bool):
        raise GoldenError(f"{where}: answerable must be a boolean")
    if obj["answerable"] != (obj["kind"] != "unanswerable"):
        raise GoldenError(f"{where}: answerable must be false exactly for kind 'unanswerable'")
    for key in ("id", "question", "reference_answer"):
        if not isinstance(obj[key], str) or not obj[key].strip():
            raise GoldenError(f"{where}: {key} must be a non-empty string")
    expected = []
    for e in obj["expected"]:
        if set(e) != {"document", "page", "quote"} or not isinstance(e["page"], int) or e["page"] < 1:
            raise GoldenError(f"{where}: expected entries are {{document, page>=1, quote}}")
        if not e["quote"].strip():
            raise GoldenError(f"{where}: every expected entry needs its source quote")
        expected.append(ExpectedPage(e["document"], e["page"], e["quote"]))
    if obj["answerable"] and not expected:
        raise GoldenError(f"{where}: an answerable row needs at least one expected page")
    if not obj["answerable"] and expected:
        raise GoldenError(f"{where}: an unanswerable row has no expected pages")
    if obj["kind"] == "multi" and len(expected) < 2:
        raise GoldenError(f"{where}: a multi row needs at least two expected pages")
    return GoldenRow(obj["id"], obj["question"], expected, obj["reference_answer"], obj["answerable"], obj["kind"])


def load_golden(path: Path = GOLDEN) -> list[GoldenRow]:
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            rows.append(parse_row(json.loads(line), i))
    ids = [r.id for r in rows]
    if len(set(ids)) != len(ids):
        raise GoldenError("golden.jsonl: duplicate ids")
    return rows
