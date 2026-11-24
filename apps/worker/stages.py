"""The idempotency + failure framework every stage task runs through (CLAUDE.md invariants).

    run_stage(ctx, stage, doc, version, handler, task, after)
      1. begin_stage (its own committed transaction, BEFORE any work):
         - stale version / terminal document          -> skip
         - cancel requested                           -> document CANCELLED, nothing enqueued
         - processed_jobs has the job key             -> duplicate: re-enqueue the next stage (covers a
                                                         crash between commit and enqueue) and stop
         - attempts >= STAGE_ATTEMPT_LIMIT            -> DLQ (attempt_limit): a poison document that
                                                         kills its worker every time stops looping
         - otherwise attempts + 1, stage RUNNING, document status forward (never backwards). Committed
           now, so a SIGKILL/OOM in the handler still counts as an attempt.
      2. handler: does the work and, in ONE transaction, upserts its output on UNIQUE keys and calls
         finish_stage, which inserts processed_jobs last (docs/adr/0002).
      3. if a cancel arrived meanwhile -> CANCELLED and stop; else `after` hook, then enqueue the next stage.
    Failures (P3, docs/adr/0003):
      - TransientError with retries left -> stage + document RETRY_WAIT, task.retry(countdown) with
        backoff + jitter, honouring Retry-After;
      - retries exhausted / PermanentError / time limit / unknown error -> final failure: stage +
        document FAILED and a dead_letters row in the same transaction, batch recomputed;
      - after any terminal transition that did not make the version active, that version's Redis keys
        are deleted (orphans), so the index only ever holds active versions (docs/adr/0004).
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
    DeadLetter,
    Document,
    DocumentStage,
    DocumentStatus,
    ProcessedJob,
    StageName,
    StageStatus,
)
from apps.api.search_index import delete_inactive_version_keys

from .context import WorkerContext
from .errors import TransientError, classify, retry_countdown

log = logging.getLogger("pagecite.worker")

TASK_NAMES = {stage: f"pagecite.{stage.value}" for stage in StageName}
NEXT_STAGE = {
    StageName.PARSE: StageName.CHUNK,
    StageName.CHUNK: StageName.EMBED,
    StageName.EMBED: StageName.INDEX,
    StageName.INDEX: None,
}
_RW = DocumentStatus.RETRY_WAIT
# (status while the stage runs, statuses it may move forward from). RETRY_WAIT -> the same stage again.
STAGE_DOC_STATUS = {
    StageName.PARSE: (DocumentStatus.PARSING, {DocumentStatus.UPLOADED, DocumentStatus.PARSING, _RW}),
    StageName.CHUNK: (DocumentStatus.CHUNKING, {DocumentStatus.PARSING, DocumentStatus.CHUNKING, _RW}),
    StageName.EMBED: (DocumentStatus.EMBEDDING, {DocumentStatus.CHUNKING, DocumentStatus.EMBEDDING, _RW}),
    StageName.INDEX: (DocumentStatus.EMBEDDING, {DocumentStatus.EMBEDDING, _RW}),  # INDEXED is set on finish
}
TERMINAL = {DocumentStatus.INDEXED, DocumentStatus.FAILED, DocumentStatus.CANCELLED}
# Statuses a failure or a retry-wait may replace: anything still in flight (not terminal, not cancelling).
IN_FLIGHT = {s for s in DocumentStatus if s not in TERMINAL and s is not DocumentStatus.CANCEL_REQUESTED}


class Begin(Enum):
    PROCEED = "proceed"
    DUPLICATE = "duplicate"
    SKIP = "skip"
    CANCEL = "cancel"
    LIMIT = "limit"


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
    filename: str = ""


def job_key(document_id: int, stage: StageName, version: int) -> str:
    return f"doc:{document_id}:{stage.value}:v{version}"


def _upsert_stage(
    session: Session, document_id: int, stage: StageName, version: int, on_insert: dict, on_update: dict
) -> None:
    """One row per (document, stage, version): INSERT ... ON DUPLICATE KEY UPDATE on that UNIQUE key."""
    stmt = insert(DocumentStage).values(document_id=document_id, stage=stage.value, version=version, **on_insert)
    session.execute(stmt.on_duplicate_key_update(**on_update))


def move_document(session: Session, doc: DocRef, new: DocumentStatus, allowed: set[DocumentStatus], **extra) -> bool:
    """Forward-only status change for the current processing version."""
    result = session.execute(
        update(Document)
        .where(
            Document.id == doc.id,
            Document.processing_version == doc.version,
            Document.status.in_([s.value for s in allowed]),
        )
        .values(status=new, **extra)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def begin_stage(
    session: Session, document_id: int, version: int, stage: StageName, attempt_limit: int = 8
) -> tuple[Begin, DocRef | None]:
    with session.begin():  # committed before the handler runs: a crash mid-handler still counts
        doc = session.get(Document, document_id, populate_existing=True)
        if doc is None or doc.processing_version != version or doc.status in TERMINAL:
            return Begin.SKIP, None
        ref = DocRef(doc.id, doc.tenant_id, doc.batch_id, version, doc.object_key, doc.mime, doc.filename)
        if doc.status == DocumentStatus.CANCEL_REQUESTED:
            return Begin.CANCEL, ref
        if session.get(ProcessedJob, job_key(document_id, stage, version)) is not None:
            return Begin.DUPLICATE, ref
        attempts = session.scalar(
            select(DocumentStage.attempts).where(
                DocumentStage.document_id == document_id,
                DocumentStage.stage == stage.value,
                DocumentStage.version == version,
            )
        )
        if attempts is not None and attempts >= attempt_limit:
            return Begin.LIMIT, ref
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
    """Counts and status from the documents. The caller must hold the batch lock (lock_batch).

    All terminal: CANCELLED if any document was cancelled, else PARTIAL if any failed, else DONE.
    Not all terminal (e.g. a replay re-opened a document): back to PROCESSING.
    """
    counts = dict(
        session.execute(
            select(Document.status, func.count()).where(Document.batch_id == batch_id).group_by(Document.status)
        ).all()
    )
    batch_total, batch_status = session.execute(
        select(Batch.total, Batch.status).where(Batch.id == batch_id)
    ).one()
    indexed = counts.get(DocumentStatus.INDEXED, 0)
    failed = counts.get(DocumentStatus.FAILED, 0)
    cancelled = counts.get(DocumentStatus.CANCELLED, 0)
    values: dict = {"succeeded": indexed, "failed": failed}
    if batch_total and indexed + failed + cancelled == batch_total:
        values["status"] = (
            BatchStatus.CANCELLED if cancelled else BatchStatus.PARTIAL if failed else BatchStatus.DONE
        )
    elif batch_status in (BatchStatus.DONE, BatchStatus.PARTIAL, BatchStatus.CANCELLED):
        values["status"] = BatchStatus.PROCESSING
    session.execute(
        update(Batch).where(Batch.id == batch_id).values(**values).execution_options(synchronize_session=False)
    )


def _cleanup_inactive_keys(ctx: WorkerContext, session: Session, doc: DocRef) -> None:
    """Delete Redis keys of every non-active version (orphans after FAILED/CANCELLED, old after a swap)."""
    if ctx.redis is None:  # DB-only tests run without Redis
        return
    with session.begin():
        delete_inactive_version_keys(session, ctx.redis, ctx.index, doc.id)


def dead_letter(
    ctx: WorkerContext, session: Session, doc: DocRef, stage: StageName, kind: str, code: str, message: str,
    retries: int = 0,
) -> None:
    """Final failure: stage + document FAILED and a dead_letters row, in one transaction; then orphan cleanup."""
    error = f"{code}: {message}"[:1000]
    with session.begin():
        lock_batch(session, doc.batch_id)
        failed = {"status": StageStatus.FAILED, "last_error": error, "finished_at": func.now(6)}
        _upsert_stage(session, doc.id, stage, doc.version, on_insert={**failed, "attempts": 1}, on_update=failed)
        move_document(session, doc, DocumentStatus.FAILED, IN_FLIGHT | {DocumentStatus.CANCEL_REQUESTED})
        attempts = session.scalar(
            select(DocumentStage.attempts).where(
                DocumentStage.document_id == doc.id,
                DocumentStage.stage == stage.value,
                DocumentStage.version == doc.version,
            )
        )
        session.add(
            DeadLetter(
                document_id=doc.id, stage=stage.value, version=doc.version, kind=kind, error_code=code,
                error=error,
                payload={"task": TASK_NAMES[stage], "args": [doc.id, doc.version], "retries": retries,
                         "attempts": attempts},
            )
        )
        recompute_batch(session, doc.batch_id)
    _cleanup_inactive_keys(ctx, session, doc)
    log.warning(
        "dead_letter document_id=%s version=%s stage=%s kind=%s code=%s retries=%s attempts=%s",
        doc.id, doc.version, stage.value, kind, code, retries, attempts,
    )


def fail_stage(session: Session, doc: DocRef, stage: StageName, code: str, message: str) -> None:
    """P2 entry point, kept for callers without a context: a permanent failure, dead-lettered."""
    from .context import get_context

    dead_letter(get_context(), session, doc, stage, "permanent", code, message)


def cancel_document(ctx: WorkerContext, session: Session, doc: DocRef, stage: StageName) -> None:
    with session.begin():
        lock_batch(session, doc.batch_id)
        cancelled = {"status": StageStatus.CANCELLED, "finished_at": func.now(6)}
        _upsert_stage(session, doc.id, stage, doc.version, on_insert={**cancelled, "attempts": 0}, on_update=cancelled)
        move_document(session, doc, DocumentStatus.CANCELLED, {DocumentStatus.CANCEL_REQUESTED})
        recompute_batch(session, doc.batch_id)
    _cleanup_inactive_keys(ctx, session, doc)


def mark_retry_wait(session: Session, doc: DocRef, stage: StageName, code: str, message: str) -> None:
    with session.begin():
        session.execute(
            update(DocumentStage)
            .where(
                DocumentStage.document_id == doc.id,
                DocumentStage.stage == stage.value,
                DocumentStage.version == doc.version,
            )
            .values(status=StageStatus.RETRY_WAIT, last_error=f"{code}: {message}"[:1000])
            .execution_options(synchronize_session=False)
        )
        move_document(session, doc, DocumentStatus.RETRY_WAIT, IN_FLIGHT)


def _cancel_requested(session: Session, doc: DocRef) -> bool:
    with session.begin():
        status = session.scalar(
            select(Document.status).where(Document.id == doc.id, Document.processing_version == doc.version)
        )
    return status == DocumentStatus.CANCEL_REQUESTED


Handler = Callable[[WorkerContext, Session, DocRef], dict]
After = Callable[[WorkerContext, Session, DocRef], None]


def run_stage(
    ctx: WorkerContext, stage: StageName, document_id: int, version: int, handler: Handler,
    task=None, after: After | None = None,
) -> str:
    """Returns the outcome (logs, tests): skipped | cancelled | duplicate | succeeded | failed.

    `task` is the bound Celery task (for retries); without it, a transient error counts as exhausted.
    `after` runs once the stage's work is committed (succeeded or duplicate), before the next enqueue.
    """
    started = time.perf_counter()
    s = ctx.settings
    session = ctx.sessions()
    next_stage = NEXT_STAGE[stage]
    detail: dict = {}
    try:
        begin, doc = begin_stage(session, document_id, version, stage, s.stage_attempt_limit)
        if begin is Begin.SKIP:
            outcome = "skipped"
        elif begin is Begin.CANCEL:
            cancel_document(ctx, session, doc, stage)
            outcome = "cancelled"
        elif begin is Begin.LIMIT:
            dead_letter(ctx, session, doc, stage, "attempt_limit", "ATTEMPT_LIMIT",
                        f"{s.stage_attempt_limit} attempts without finishing (worker killed or crashing?)")
            outcome, detail = "failed", {"code": "ATTEMPT_LIMIT"}
        elif begin is Begin.DUPLICATE:
            outcome = "duplicate"
        else:
            try:
                detail = handler(ctx, session, doc) or {}
                outcome = "succeeded"
            except DuplicateJob:
                session.rollback()
                outcome = "duplicate"
            except SoftTimeLimitExceeded:
                session.rollback()
                dead_letter(ctx, session, doc, stage, "permanent", "TIME_LIMIT", "the stage exceeded its time limit")
                outcome, detail = "failed", {"code": "TIME_LIMIT"}
            except Exception as exc:
                session.rollback()
                err = classify(exc)
                retries = task.request.retries if task is not None else 0
                if isinstance(err, TransientError):
                    if task is not None and retries < task.max_retries:
                        countdown = retry_countdown(retries, err.retry_after, s.retry_backoff_max_s)
                        mark_retry_wait(session, doc, stage, err.code, err.message)
                        log.info(
                            "stage stage=%s document_id=%s version=%s outcome=retry code=%s retry=%s/%s "
                            "countdown_s=%s retry_after=%s",
                            stage.value, document_id, version, err.code, retries + 1, task.max_retries,
                            countdown, err.retry_after,
                        )
                        session.close()
                        raise task.retry(exc=err, countdown=countdown) from exc
                    dead_letter(ctx, session, doc, stage, "exhausted", err.code, err.message, retries)
                else:
                    if err.code == "UNEXPECTED":
                        log.error(
                            "stage_error stage=%s document_id=%s version=%s type=%s",
                            stage.value, document_id, version, type(exc).__name__, exc_info=True,
                        )
                    dead_letter(ctx, session, doc, stage, "permanent", err.code, err.message, retries)
                outcome, detail = "failed", {"code": err.code}

        if outcome == "succeeded" and _cancel_requested(session, doc):
            # Cancel arrived while the handler ran: its output is committed, but nothing continues.
            cancel_document(ctx, session, doc, stage)
            outcome = "cancelled"
        if outcome in ("succeeded", "duplicate"):
            if after is not None:
                after(ctx, session, doc)
            # Duplicate included: the first run may have died between its commit and this enqueue.
            if next_stage is not None:
                ctx.enqueue(TASK_NAMES[next_stage], document_id, version)
    finally:
        session.close()

    extra = " ".join(f"{k}={v}" for k, v in detail.items())
    log.info(
        "stage stage=%s document_id=%s version=%s outcome=%s ms=%.0f %s",
        stage.value, document_id, version, outcome, (time.perf_counter() - started) * 1000, extra,
    )
    return outcome
