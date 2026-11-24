"""ORM mappings for the tables P1 uses. Flyway owns the DDL (db/migrations); these only map it."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, String, Text
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
