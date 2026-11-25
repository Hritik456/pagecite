"""ORM mappings for the tables P1 uses. Flyway owns the DDL (db/migrations); these only map it."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, LargeBinary, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import FetchedValue


class Base(DeclarativeBase):
    pass


class BatchStatus(StrEnum):
    UPLOADING = "UPLOADING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"


class DocumentStatus(StrEnum):
    CREATED = "CREATED"
    UPLOADED = "UPLOADED"
    PARSING = "PARSING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXED = "INDEXED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class StageName(StrEnum):
    PARSE = "parse"
    CHUNK = "chunk"
    EMBED = "embed"
    INDEX = "index"


class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_DOCUMENT_STATUSES = frozenset({DocumentStatus.INDEXED, DocumentStatus.FAILED, DocumentStatus.CANCELLED})


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    api_key_hash: Mapped[str] = mapped_column(String(64))
    max_file_mb: Mapped[int]
    max_batch_files: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=FetchedValue())


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tenants.id"))
    status: Mapped[str] = mapped_column(String(16), default=BatchStatus.UPLOADING)
    total: Mapped[int]
    succeeded: Mapped[int] = mapped_column(default=0)
    failed: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=FetchedValue())
    updated_at: Mapped[datetime] = mapped_column(server_default=FetchedValue(), server_onupdate=FetchedValue())


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("batches.id"))
    tenant_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tenants.id"))
    object_key: Mapped[str] = mapped_column(String(512))
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime: Mapped[str] = mapped_column(String(127))
    status: Mapped[str] = mapped_column(String(24), default=DocumentStatus.CREATED)
    processing_version: Mapped[int] = mapped_column(default=1)
    # The version retrieval serves (V3). Switched in the transaction that marks a version INDEXED.
    active_version: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=FetchedValue())
    updated_at: Mapped[datetime] = mapped_column(server_default=FetchedValue(), server_onupdate=FetchedValue())


class DocumentStage(Base):
    __tablename__ = "document_stages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("documents.id"))
    stage: Mapped[str] = mapped_column(String(8))
    version: Mapped[int]
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(server_default=FetchedValue())
    published_at: Mapped[datetime | None]


class DocumentPage(Base):
    """Parse output: text per page (V2)."""

    __tablename__ = "document_pages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("documents.id"))
    version: Mapped[int]
    page_no: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    ocr: Mapped[bool] = mapped_column(default=False)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("documents.id"))
    tenant_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tenants.id"))
    version: Mapped[int]
    ord: Mapped[int]
    page_from: Mapped[int]
    page_to: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int]
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)
    embedding_model: Mapped[str | None] = mapped_column(String(64))
    embedding_done: Mapped[bool] = mapped_column(default=False)


class ProcessedJob(Base):
    __tablename__ = "processed_jobs"

    job_key: Mapped[str] = mapped_column(String(191), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(server_default=FetchedValue())


class DeadLetter(Base):
    """A final failure (V1 + V3 kind/error_code). Replay marks replayed_at."""

    __tablename__ = "dead_letters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("documents.id"))
    stage: Mapped[str] = mapped_column(String(8))
    version: Mapped[int]
    kind: Mapped[str] = mapped_column(String(16))  # exhausted | permanent | attempt_limit
    error_code: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(server_default=FetchedValue())
    replayed_at: Mapped[datetime | None]


class EvalRun(Base):
    """One config's result over the golden set (V1 + V4). Written by `python -m eval.run`."""

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    label: Mapped[str | None] = mapped_column(String(64))
    n_questions: Mapped[int]
    hit_at_5: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    mrr: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    faithfulness: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    abstention_accuracy: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    false_abstain_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    avg_tokens: Mapped[Decimal | None] = mapped_column(Numeric(10, 1))
    avg_latency_ms: Mapped[Decimal | None] = mapped_column(Numeric(10, 1))
    n_errors: Mapped[int] = mapped_column(default=0)
    est_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    git_sha: Mapped[str | None] = mapped_column(String(40))
    judge_model: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=FetchedValue())
