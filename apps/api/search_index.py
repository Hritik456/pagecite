"""Redis 8.4 Query Engine index over chunk hashes.

The index is *derived*: MySQL holds the truth (chunk text and, since V2, the embedding), and
`rebuild()` (CLI: `flask --app apps.api reindex`) can always recreate it. Keys are
`{prefix}{tenant}:{chunkId}` with fields tenant TAG, doc_id TAG, text TEXT, page NUMERIC and
embedding VECTOR (HNSW, FLOAT32, COSINE). Used by the index worker, reindex and (P4) retrieval.
"""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

import redis
from redis.exceptions import ResponseError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import Chunk, Document, DocumentStatus


@dataclass(frozen=True)
class IndexConfig:
    name: str
    prefix: str
    dim: int


@dataclass(frozen=True)
class ChunkRecord:
    tenant_id: int
    document_id: int
    chunk_id: int
    text: str
    page: int
    embedding: bytes


@dataclass(frozen=True)
class Fingerprint:
    digest: str
    keys: int
    per_tenant: dict[int, int]


def index_config(settings: Settings) -> IndexConfig:
    return IndexConfig(name=settings.index_name, prefix=settings.index_prefix, dim=settings.embed_dim)


def make_redis(url: str) -> redis.Redis:
    # Binary-safe: the embedding field is raw float32 bytes. RESP2 pins the FT.* reply shapes
    # (redis-py 8 defaults to RESP3, where FT.SEARCH returns a map instead of a flat list).
    return redis.Redis.from_url(url, decode_responses=False, protocol=2)


def chunk_key(cfg: IndexConfig, tenant_id: int, chunk_id: int) -> str:
    return f"{cfg.prefix}{tenant_id}:{chunk_id}"


def index_exists(r: redis.Redis, cfg: IndexConfig) -> bool:
    try:
        r.execute_command("FT.INFO", cfg.name)
        return True
    except ResponseError as exc:
        msg = str(exc).lower()
        if "unknown index" in msg or "no such index" in msg:
            return False
        raise


def ensure_index(r: redis.Redis, cfg: IndexConfig) -> bool:
    """Create the index if it does not exist. Returns True if it was created."""
    if index_exists(r, cfg):
        return False
    try:
        r.execute_command(
            "FT.CREATE", cfg.name, "ON", "HASH", "PREFIX", "1", cfg.prefix,
            "SCHEMA",
            "tenant", "TAG",
            "doc_id", "TAG",
            "text", "TEXT",
            "page", "NUMERIC",
            "embedding", "VECTOR", "HNSW", "6",
            "TYPE", "FLOAT32", "DIM", str(cfg.dim), "DISTANCE_METRIC", "COSINE",
        )
    except ResponseError as exc:
        if "already exists" in str(exc).lower():  # another process won the race
            return False
        raise
    return True


def drop_index(r: redis.Redis, cfg: IndexConfig) -> None:
    """Drop the index definition only (keys stay). Used by tests."""
    try:
        r.execute_command("FT.DROPINDEX", cfg.name)
    except ResponseError as exc:
        if "unknown index" not in str(exc).lower() and "no such index" not in str(exc).lower():
            raise


def write_chunks(r: redis.Redis, cfg: IndexConfig, records: Iterable[ChunkRecord], batch: int = 500) -> int:
    """HSET one hash per chunk (idempotent: same chunk -> same key and fields)."""
    written = 0
    pipe = r.pipeline(transaction=False)
    for rec in records:
        pipe.hset(
            chunk_key(cfg, rec.tenant_id, rec.chunk_id),
            mapping={
                "tenant": str(rec.tenant_id),
                "doc_id": str(rec.document_id),
                "text": rec.text,
                "page": rec.page,
                "embedding": rec.embedding,
            },
        )
        written += 1
        if written % batch == 0:
            pipe.execute()
    pipe.execute()
    return written


def _delete_matching(r: redis.Redis, pattern: str) -> int:
    deleted = 0
    batch: list[bytes] = []
    for key in r.scan_iter(match=pattern, count=1000):
        batch.append(key)
        if len(batch) >= 500:
            deleted += r.unlink(*batch)
            batch.clear()
    if batch:
        deleted += r.unlink(*batch)
    return deleted


def delete_tenant_keys(r: redis.Redis, cfg: IndexConfig, tenant_id: int) -> int:
    return _delete_matching(r, f"{cfg.prefix}{tenant_id}:*")


def delete_all_keys(r: redis.Redis, cfg: IndexConfig) -> int:
    return _delete_matching(r, f"{cfg.prefix}*")


def count_tenant(r: redis.Redis, cfg: IndexConfig, tenant_id: int) -> int:
    """Number of indexed chunks for a tenant, as the index itself sees them."""
    reply = r.execute_command(
        "FT.SEARCH", cfg.name, f"@tenant:{{{tenant_id}}}", "LIMIT", "0", "0", "DIALECT", "2"
    )
    return int(reply[0])


_QUERY_SPECIAL = re.compile(r"([^A-Za-z0-9 ])")


def search_phrase(r: redis.Redis, cfg: IndexConfig, tenant_id: int, phrase: str, limit: int = 10) -> list[int]:
    """Document ids whose chunks contain the exact phrase, within one tenant (filter inside the query)."""
    escaped = _QUERY_SPECIAL.sub(r"\\\1", phrase.strip())
    reply = r.execute_command(
        "FT.SEARCH", cfg.name, f'@tenant:{{{tenant_id}}} @text:"{escaped}"',
        "RETURN", "1", "doc_id", "LIMIT", "0", str(limit), "DIALECT", "2",
    )
    doc_ids: list[int] = []
    # reply = [total, key1, [field, value], key2, [field, value], ...]
    for fields in reply[2::2]:
        values = dict(zip(fields[::2], fields[1::2]))
        doc_ids.append(int(values[b"doc_id"]))
    return doc_ids


def _tenant_of(cfg: IndexConfig, key: bytes) -> int:
    return int(key.decode()[len(cfg.prefix):].split(":", 1)[0])


def fingerprint(r: redis.Redis, cfg: IndexConfig, tenant_id: int | None = None) -> Fingerprint:
    """SHA-256 over every key (one tenant, or the whole prefix) and all of its fields, in sorted order."""
    pattern = f"{cfg.prefix}{tenant_id}:*" if tenant_id is not None else f"{cfg.prefix}*"
    keys = sorted(r.scan_iter(match=pattern, count=1000))
    h = hashlib.sha256()
    per_tenant: dict[int, int] = {}
    for key in keys:
        per_tenant[_tenant_of(cfg, key)] = per_tenant.get(_tenant_of(cfg, key), 0) + 1
        h.update(len(key).to_bytes(4, "big") + key)
        for field, value in sorted(r.hgetall(key).items()):
            h.update(len(field).to_bytes(4, "big") + field + len(value).to_bytes(4, "big") + value)
    return Fingerprint(digest=h.hexdigest(), keys=len(keys), per_tenant=dict(sorted(per_tenant.items())))


def indexable_chunks_query(tenant_id: int | None = None):
    """Chunks that belong in the index: current version of INDEXED documents, embedding stored."""
    q = (
        select(Chunk.tenant_id, Chunk.document_id, Chunk.id, Chunk.text, Chunk.page_from, Chunk.embedding)
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Chunk.version == Document.processing_version,
            Document.status == DocumentStatus.INDEXED,
            Chunk.embedding_done.is_(True),
        )
        .order_by(Chunk.tenant_id, Chunk.id)
    )
    if tenant_id is not None:
        q = q.where(Chunk.tenant_id == tenant_id)
    return q


def rebuild(session: Session, r: redis.Redis, cfg: IndexConfig, tenant_id: int | None = None) -> dict[int, int]:
    """Recreate the index from MySQL for one tenant, or for every tenant. Returns chunks written per tenant."""
    ensure_index(r, cfg)
    if tenant_id is None:
        delete_all_keys(r, cfg)
    else:
        delete_tenant_keys(r, cfg, tenant_id)

    written: dict[int, int] = {}

    def records() -> Iterable[ChunkRecord]:
        result = session.execute(indexable_chunks_query(tenant_id).execution_options(yield_per=500))
        for row in result:
            written[row.tenant_id] = written.get(row.tenant_id, 0) + 1
            yield ChunkRecord(row.tenant_id, row.document_id, row.id, row.text, row.page_from, row.embedding)

    write_chunks(r, cfg, records())
    return written
