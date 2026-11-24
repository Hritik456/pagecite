"""The idempotency framework every stage task runs through (CLAUDE.md invariants).

    run_stage(stage, doc, version, handler, next_task)
      1. begin_stage: skip stale/terminal documents; if processed_jobs already has the job key the work
         is done -> re-enqueue the next stage (covers a crash between commit and enqueue) and stop;
         otherwise mark the stage RUNNING and move the document status forward (never backwards).
      2. handler: does the work and, in ONE transaction, upserts its output on UNIQUE keys and calls
         finish_stage, which inserts processed_jobs last (see docs/adr/0002 for why last, not first).
      3. enqueue the next stage after the commit.
    PermanentError (and, in P2, any other error) -> stage + document FAILED, batch recomputed, no retry.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, select, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.models import (
    Batch,
    BatchStatus,
    Document,
    DocumentStage,
    DocumentStatus,
    ProcessedJob,
    StageName,
    StageStatus,
)

from .context import WorkerContext
from .errors import PermanentError

log = logging.getLogger("pagecite.worker")

TASK_NAMES = {stage: f"pagecite.{stage.value}" for stage in StageName}
NEXT_STAGE = {
    StageName.PARSE: StageName.CHUNK,
    StageName.CHUNK: StageName.EMBED,
    StageName.EMBED: StageName.INDEX,
    StageName.INDEX: None,
}
# (status while the stage runs, statuses it may move forward from)
STAGE_DOC_STATUS = {
    StageName.PARSE: (DocumentStatus.PARSING, {DocumentStatus.UPLOADED, DocumentStatus.PARSING}),
    StageName.CHUNK: (DocumentStatus.CHUNKING, {DocumentStatus.PARSING, DocumentStatus.CHUNKING}),
    StageName.EMBED: (DocumentStatus.EMBEDDING, {DocumentStatus.CHUNKING, DocumentStatus.EMBEDDING}),
    StageName.INDEX: (DocumentStatus.EMBEDDING, {DocumentStatus.EMBEDDING}),  # INDEXED is set on finish
}
SKIP_STATUSES = {
    DocumentStatus.INDEXED, DocumentStatus.FAILED, DocumentStatus.CANCEL_REQUESTED, DocumentStatus.CANCELLED,
}


class Begin(Enum):
    PROCEED = "proceed"
    DUPLICATE = "duplicate"
    SKIP = "skip"


class DuplicateJob(Exception):
    """processed_jobs already had the key at commit time: a concurrent duplicate finished first."""


@dataclass(frozen=True)
class DocRef:
    id: int
    tenant_id: int
    batch_id: int
    version: int
    object_key: str
    mime: str


def job_key(document_id: int, stage: StageName, version: int) -> str:
    return f"doc:{document_id}:{stage.value}:v{version}"


def _upsert_stage(
    session: Session, document_id: int, stage: StageName, version: int, on_insert: dict, on_update: dict
) -> None:
    """One row per (document, stage, version): INSERT ... ON DUPLICATE KEY UPDATE on that UNIQUE key."""
    stmt = insert(DocumentStage).values(document_id=document_id, stage=stage.value, version=version, **on_insert)
    session.execute(stmt.on_duplicate_key_update(**on_update))


def move_document(session: Session, doc: DocRef, new: DocumentStatus, allowed: set[DocumentStatus]) -> bool:
    """Forward-only status change for the current processing version."""
    result = session.execute(
        update(Document)
        .where(
            Document.id == doc.id,
            Document.processing_version == doc.version,
            Document.status.in_([s.value for s in allowed]),
        )
        .values(status=new)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def begin_stage(session: Session, document_id: int, version: int, stage: StageName) -> tuple[Begin, DocRef | None]:
    with session.begin():
        doc = session.get(Document, document_id, populate_existing=True)
        if doc is None or doc.processing_version != version or doc.status in SKIP_STATUSES:
            return Begin.SKIP, None
        ref = DocRef(doc.id, doc.tenant_id, doc.batch_id, version, doc.object_key, doc.mime)
        if session.get(ProcessedJob, job_key(document_id, stage, version)) is not None:
            return Begin.DUPLICATE, ref
        _upsert_stage(
            session, document_id, stage, version,
            on_insert={"status": StageStatus.RUNNING, "attempts": 1, "started_at": func.now(6)},
            on_update={
                "status": StageStatus.RUNNING,
                "attempts": DocumentStage.attempts + 1,
                "started_at": func.now(6),
                "finished_at": None,
                "last_error": None,
            },
        )
        running, allowed = STAGE_DOC_STATUS[stage]
        move_document(session, ref, running, allowed)
    return Begin.PROCEED, ref


def finish_stage(session: Session, doc: DocRef, stage: StageName) -> None:
    """Call inside the handler's output transaction, after the output upserts."""
    session.execute(
        update(DocumentStage)
        .where(
            DocumentStage.document_id == doc.id,
            DocumentStage.stage == stage.value,
            DocumentStage.version == doc.version,
        )
        .values(status=StageStatus.SUCCEEDED, finished_at=func.now(6))
        .execution_options(synchronize_session=False)
    )
    try:
        with session.begin_nested():
            session.execute(insert(ProcessedJob).values(job_key=job_key(doc.id, stage, doc.version)))
    except IntegrityError as exc:
        raise DuplicateJob(job_key(doc.id, stage, doc.version)) from exc


def lock_batch(session: Session, batch_id: int) -> None:
    """Take the batch row lock first, the same order as POST /documents/:id/complete, so no deadlock."""
    session.execute(select(Batch.id).where(Batch.id == batch_id).with_for_update())


def recompute_batch(session: Session, batch_id: int) -> None:
    """succeeded/failed counts; DONE when every document is INDEXED, PARTIAL when all ended and some FAILED.

    The caller must already hold the batch lock (lock_batch) in this transaction.
    """
    counts = dict(
        session.execute(
            select(Document.status, func.count()).where(Document.batch_id == batch_id).group_by(Document.status)
        ).all()
    )
    total = session.scalar(select(Batch.total).where(Batch.id == batch_id)) or 0
    indexed = counts.get(DocumentStatus.INDEXED, 0)
    failed = counts.get(DocumentStatus.FAILED, 0)
    cancelled = counts.get(DocumentStatus.CANCELLED, 0)
    values: dict = {"succeeded": indexed, "failed": failed}
    if total and indexed + failed + cancelled == total:
        values["status"] = BatchStatus.DONE if failed == 0 and cancelled == 0 else BatchStatus.PARTIAL
    session.execute(
        update(Batch).where(Batch.id == batch_id).values(**values).execution_options(synchronize_session=False)
    )


def fail_stage(session: Session, doc: DocRef, stage: StageName, code: str, message: str) -> None:
    with session.begin():
        lock_batch(session, doc.batch_id)
        failed = {
            "status": StageStatus.FAILED,
            "last_error": f"{code}: {message}"[:1000],
            "finished_at": func.now(6),
        }
        _upsert_stage(session, doc.id, stage, doc.version, on_insert={**failed, "attempts": 1}, on_update=failed)
        move_document(
            session, doc, DocumentStatus.FAILED,
            {s for s in DocumentStatus if s not in SKIP_STATUSES},
        )
        recompute_batch(session, doc.batch_id)


Handler = Callable[[WorkerContext, Session, DocRef], dict]


def run_stage(ctx: WorkerContext, stage: StageName, document_id: int, version: int, handler: Handler) -> str:
    """Returns the outcome (for logs and tests): skipped | duplicate | succeeded | failed."""
    started = time.perf_counter()
    session = ctx.sessions()
    next_stage = NEXT_STAGE[stage]
    try:
        begin, doc = begin_stage(session, document_id, version, stage)
        if begin is Begin.SKIP:
            outcome, detail = "skipped", {}
        elif begin is Begin.DUPLICATE:
            outcome, detail = "duplicate", {}
        else:
            try:
                detail = handler(ctx, session, doc) or {}
                outcome = "succeeded"
            except DuplicateJob:
                session.rollback()
                outcome, detail = "duplicate", {}
            except SoftTimeLimitExceeded:
                session.rollback()
                fail_stage(session, doc, stage, "TIME_LIMIT", "the stage exceeded its time limit")
                outcome, detail = "failed", {"code": "TIME_LIMIT"}
            except PermanentError as exc:
                session.rollback()
                fail_stage(session, doc, stage, exc.code, exc.message)
                outcome, detail = "failed", {"code": exc.code}
            except Exception as exc:  # P2: unexpected errors fail too; P3 adds TransientError retries
                session.rollback()
                log.error(
                    "stage_error stage=%s document_id=%s version=%s type=%s",
                    stage.value, document_id, version, type(exc).__name__, exc_info=True,
                )
                fail_stage(session, doc, stage, "UNEXPECTED", f"{type(exc).__name__}: {str(exc)[:300]}")
                outcome, detail = "failed", {"code": "UNEXPECTED"}

        # Duplicate included: the first run may have died between its commit and this enqueue.
        if outcome in ("succeeded", "duplicate") and next_stage is not None:
            ctx.enqueue(TASK_NAMES[next_stage], document_id, version)
    finally:
        session.close()

    extra = " ".join(f"{k}={v}" for k, v in detail.items())
    log.info(
        "stage stage=%s document_id=%s version=%s outcome=%s ms=%.0f %s",
        stage.value, document_id, version, outcome, (time.perf_counter() - started) * 1000, extra,
    )
    return outcome
