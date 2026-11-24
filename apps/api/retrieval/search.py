"""Index queries: FT.HYBRID (text + vector, RRF) and KNN-only, both tenant pre-filtered.

FT.HYBRID is sent with execute_command (redis-py's hybrid helper is experimental) and its RESP2
reply is parsed by parse_hybrid_reply(), a small pure function with unit tests.

Tenant isolation: the @tenant filter is part of every index query (the SEARCH clause, the VSIM
FILTER and the KNN pre-filter), so another tenant's chunks are never candidates. Results are not
filtered afterwards; a foreign hit raises instead, because it would mean the query itself is wrong.
"""

from dataclasses import dataclass

import redis
from redis.exceptions import ResponseError

from ..search_index import IndexConfig


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    tenant_id: int
    document_id: int
    page: int
    score: float  # higher is better: RRF score, or 1 - cosine distance for KNN


class IndexUnavailable(Exception):
    """Redis is down or the index does not exist (spec §8: queries return 503; `reindex` rebuilds)."""


class ForeignTenantHit(RuntimeError):
    """An index query returned another tenant's chunk. Never filtered away silently."""


def _key_ids(cfg: IndexConfig, key: bytes | str) -> tuple[int, int]:
    text = key.decode() if isinstance(key, bytes) else key
    tenant, chunk = text[len(cfg.prefix):].split(":", 1)
    return int(tenant), int(chunk)


def _fields(flat: list) -> dict[str, bytes]:
    return {(k.decode() if isinstance(k, bytes) else k): v for k, v in zip(flat[::2], flat[1::2])}


def parse_hybrid_reply(reply: list, cfg: IndexConfig) -> list[Hit]:
    """RESP2: [b'total_results', n, b'results', [[field, value, ...], ...], b'warnings', [...], ...]."""
    top = _fields(reply)
    hits = []
    for row in top.get("results") or []:
        f = _fields(row)
        tenant_id, chunk_id = _key_ids(cfg, f["__key"])
        hits.append(
            Hit(
                chunk_id=chunk_id,
                tenant_id=tenant_id,
                document_id=int(f["doc_id"]),
                page=int(float(f.get("page", b"0"))),
                score=float(f.get("rrf", b"0")),
            )
        )
    return hits


def parse_knn_reply(reply: list, cfg: IndexConfig) -> list[Hit]:
    """FT.SEARCH RESP2: [total, key1, [field, value, ...], key2, [...], ...] with a `dist` field."""
    hits = []
    for key, flat in zip(reply[1::2], reply[2::2]):
        f = _fields(flat)
        tenant_id, chunk_id = _key_ids(cfg, key)
        hits.append(
            Hit(
                chunk_id=chunk_id,
                tenant_id=tenant_id,
                document_id=int(f["doc_id"]),
                page=int(float(f.get("page", b"0"))),
                score=1.0 - float(f.get("dist", b"1")),
            )
        )
    return hits


def _guard(hits: list[Hit], tenant_id: int) -> list[Hit]:
    foreign = [h for h in hits if h.tenant_id != tenant_id]
    if foreign:
        raise ForeignTenantHit(f"{len(foreign)} hit(s) from another tenant in a tenant-filtered query")
    return hits


def _run(r: redis.Redis, *args) -> list:
    try:
        return r.execute_command(*args)
    except redis.ConnectionError as exc:
        raise IndexUnavailable("redis unreachable") from exc
    except ResponseError as exc:
        msg = str(exc).lower()
        if "no such index" in msg or "unknown index" in msg:
            raise IndexUnavailable("search index missing") from exc
        raise


def hybrid_search(
    r: redis.Redis, cfg: IndexConfig, tenant_id: int, text_q: str, vector: bytes, k: int
) -> list[Hit]:
    tenant = f"@tenant:{{{tenant_id}}}"
    reply = _run(
        r, "FT.HYBRID", cfg.name,
        "SEARCH", f"{tenant} ({text_q})",
        "VSIM", "@embedding", "$vec", "KNN", "2", "K", str(k), "FILTER", tenant,
        "COMBINE", "RRF", "6", "CONSTANT", "60", "WINDOW", str(k), "YIELD_SCORE_AS", "rrf",
        "LIMIT", "0", str(k),
        "LOAD", "3", "@__key", "@doc_id", "@page",
        "PARAMS", "2", "vec", vector,
    )
    return _guard(parse_hybrid_reply(reply, cfg), tenant_id)


def vector_search(r: redis.Redis, cfg: IndexConfig, tenant_id: int, vector: bytes, k: int) -> list[Hit]:
    reply = _run(
        r, "FT.SEARCH", cfg.name,
        f"(@tenant:{{{tenant_id}}})=>[KNN {k} @embedding $vec AS dist]",
        "PARAMS", "2", "vec", vector,
        "RETURN", "3", "doc_id", "page", "dist",
        "SORTBY", "dist", "LIMIT", "0", str(k), "DIALECT", "2",
    )
    return _guard(parse_knn_reply(reply, cfg), tenant_id)


def merge_hits(result_lists: list[list[Hit]], limit: int) -> list[Hit]:
    """De-duplicate by chunk across sub-queries, keep each chunk's best score, best first (ties: first seen)."""
    best: dict[int, tuple[Hit, int]] = {}
    order = 0
    for hits in result_lists:
        for hit in hits:
            current = best.get(hit.chunk_id)
            if current is None:
                best[hit.chunk_id] = (hit, order)
                order += 1
            elif hit.score > current[0].score:
                best[hit.chunk_id] = (hit, current[1])
    ranked = sorted(best.values(), key=lambda pair: (-pair[0].score, pair[1]))
    return [hit for hit, _ in ranked[:limit]]
