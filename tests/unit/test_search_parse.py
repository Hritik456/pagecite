import pytest

from apps.api.retrieval.search import Hit, merge_hits, parse_hybrid_reply, parse_knn_reply
from apps.api.search_index import IndexConfig

CFG = IndexConfig(name="idx:chunks", prefix="chunk:", dim=1536)

# Recorded from Redis 8.4.8 (RESP2): FT.HYBRID ... COMBINE RRF ... YIELD_SCORE_AS rrf LOAD 3 @__key @doc_id @page
REAL_HYBRID_REPLY = [
    b"total_results", 20,
    b"results", [
        [b"__key", b"chunk:4:211", b"doc_id", b"18", b"page", b"2", b"rrf", b"0.0297267759563"],
        [b"__key", b"chunk:4:224", b"doc_id", b"21", b"page", b"1", b"rrf", b"0.016393442623"],
        [b"__key", b"chunk:4:207", b"doc_id", b"18", b"page", b"1", b"rrf", b"0.0161290322581"],
    ],
    b"warnings", [],
    b"execution_time", b"3.80932",
]


def test_parse_real_hybrid_reply():
    hits = parse_hybrid_reply(REAL_HYBRID_REPLY, CFG)
    assert hits[0] == Hit(chunk_id=211, tenant_id=4, document_id=18, page=2, score=pytest.approx(0.0297267759563))
    assert [h.chunk_id for h in hits] == [211, 224, 207]
    assert all(h.tenant_id == 4 for h in hits)


def test_parse_empty_hybrid_reply():
    reply = [b"total_results", 0, b"results", [], b"warnings", [], b"execution_time", b"0.1"]
    assert parse_hybrid_reply(reply, CFG) == []


def test_missing_score_and_page_default_to_zero():
    reply = [b"total_results", 1, b"results", [[b"__key", b"chunk:1:5", b"doc_id", b"2"]]]
    assert parse_hybrid_reply(reply, CFG) == [Hit(5, 1, 2, 0, 0.0)]


def test_custom_prefix_with_colons():
    cfg = IndexConfig(name="idx:test_chunks", prefix="test:chunk:", dim=8)
    reply = [b"total_results", 1, b"results", [[b"__key", b"test:chunk:12:99", b"doc_id", b"3", b"page", b"4"]]]
    hit = parse_hybrid_reply(reply, cfg)[0]
    assert (hit.chunk_id, hit.tenant_id) == (99, 12)


def test_parse_knn_reply_turns_distance_into_score():
    reply = [
        2,
        b"chunk:4:224", [b"dist", b"5.96046447754e-08", b"doc_id", b"21", b"page", b"1"],
        b"chunk:4:207", [b"dist", b"0.519041776657", b"doc_id", b"18", b"page", b"1"],
    ]
    hits = parse_knn_reply(reply, CFG)
    assert [h.chunk_id for h in hits] == [224, 207]
    assert hits[0].score == pytest.approx(1.0) and hits[1].score == pytest.approx(0.480958, abs=1e-5)


def test_merge_keeps_best_score_per_chunk_and_order():
    a = [Hit(1, 4, 10, 1, 0.03), Hit(2, 4, 10, 1, 0.02)]
    b = [Hit(2, 4, 10, 1, 0.05), Hit(3, 4, 11, 2, 0.02)]
    merged = merge_hits([a, b], limit=10)
    assert [(h.chunk_id, h.score) for h in merged] == [(2, 0.05), (1, 0.03), (3, 0.02)]


def test_merge_ties_keep_first_seen_and_limit():
    merged = merge_hits([[Hit(i, 1, 1, 1, 0.01) for i in range(30)]], limit=20)
    assert [h.chunk_id for h in merged] == list(range(20))
