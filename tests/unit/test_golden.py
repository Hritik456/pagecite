"""eval/golden.jsonl stays well-formed and covers what P5 needs. Runs without the PDFs (CI)."""

import json

import pytest

from apps.api.retrieval.decompose import needs_decomposition
from eval.build_corpus import load_manifest
from eval.golden import MAX_ROWS, MIN_ROWS, GoldenError, load_golden, parse_row

ROWS = load_golden()
CORPUS = {f["file"]: f for f in load_manifest()["files"]}

# The near-duplicate circular families named in P5: (circular numbers, file-name prefixes).
TWIN_FAMILIES = {
    "KYC 257-262": ("13699", "13700", "13701", "13702", "13703", "13704"),
    "capital adequacy 286-289": ("13732", "13733", "13734", "13735"),
    "securitisation 304-307": ("13753", "13754", "13755", "13756"),
    "shares/voting rights 275-278": ("13719", "13720", "13721", "13722"),
}


def test_row_count_and_unanswerable_share():
    assert MIN_ROWS <= len(ROWS) <= MAX_ROWS
    assert 4 <= sum(not r.answerable for r in ROWS) <= 6


def test_every_expected_page_exists_in_the_pinned_corpus():
    for row in ROWS:
        for e in row.expected:
            assert e.document in CORPUS, f"{row.id}: {e.document}"
            assert 1 <= e.page <= CORPUS[e.document]["pages"], f"{row.id}: p.{e.page}"


@pytest.mark.parametrize("family", sorted(TWIN_FAMILIES))
def test_each_twin_family_is_targeted_by_an_identifier_or_bank_type_row(family):
    prefixes = TWIN_FAMILIES[family]
    rows = [r for r in ROWS if r.kind in ("identifier", "bank_type")
            and any(e.document.startswith(prefixes) for e in r.expected)]
    assert rows, f"no identifier/bank_type row targets the {family} twins"


def test_multi_rows_trigger_decomposition():
    # Otherwise hybrid_rerank_decomp would run exactly like hybrid_rerank on them.
    for row in ROWS:
        if row.kind == "multi":
            assert needs_decomposition(row.question), row.id


def test_kinds_are_all_present():
    assert {r.kind for r in ROWS} == {"semantic", "identifier", "bank_type", "multi", "unanswerable"}


GOOD = {"id": "x", "question": "q?", "expected": [{"document": "a.pdf", "page": 1, "quote": "text"}],
        "reference_answer": "a", "answerable": True, "kind": "semantic"}


@pytest.mark.parametrize("bad", [
    {"kind": "trivia"},
    {"answerable": False},  # semantic must be answerable
    {"expected": []},
    {"expected": [{"document": "a.pdf", "page": 0, "quote": "t"}]},
    {"expected": [{"document": "a.pdf", "page": 1, "quote": " "}]},
    {"kind": "multi"},  # needs two expected pages
    {"extra": 1},
])
def test_schema_rejects_bad_rows(bad):
    with pytest.raises(GoldenError):
        parse_row({**GOOD, **bad}, 1)


def test_unanswerable_rows_have_no_pages():
    row = {**GOOD, "kind": "unanswerable", "answerable": False}
    with pytest.raises(GoldenError):
        parse_row(row, 1)
    assert parse_row({**row, "expected": []}, 1).expected == []


def test_duplicate_ids_are_rejected(tmp_path):
    path = tmp_path / "g.jsonl"
    path.write_text("\n".join(json.dumps(GOOD) for _ in range(2)), encoding="utf-8")
    with pytest.raises(GoldenError, match="duplicate"):
        load_golden(path)
