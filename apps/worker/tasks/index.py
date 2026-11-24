"""Stage 4 - index: chunks (text + embedding from MySQL) -> one Redis hash per chunk; document INDEXED."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.models import Chunk, DocumentStatus, StageName
from apps.api.search_index import ChunkRecord, ensure_index, write_chunks

from ..celery_app import STAGE_TASK_OPTIONS, app
from ..context import WorkerContext, get_context
from ..stages import DocRef, finish_stage, lock_batch, move_document, recompute_batch, run_stage


def handle(ctx: WorkerContext, session: Session, doc: DocRef) -> dict:
    if not ctx.index_ready:
        ensure_index(ctx.redis, ctx.index)
        ctx.index_ready = True

    with session.begin():
        rows = session.execute(
            select(Chunk.id, Chunk.text, Chunk.page_from, Chunk.embedding, Chunk.embedding_done)
            .where(Chunk.document_id == doc.id, Chunk.version == doc.version)
            .order_by(Chunk.ord)
        ).all()
    missing = [r.id for r in rows if not r.embedding_done or r.embedding is None]
    if missing or not rows:
        raise RuntimeError(f"{len(missing)} of {len(rows)} chunks have no embedding")

    # Same records `reindex` builds from MySQL, so the index is identical whichever wrote it.
    written = write_chunks(
        ctx.redis,
        ctx.index,
        (ChunkRecord(doc.tenant_id, doc.id, r.id, r.text, r.page_from, r.embedding) for r in rows),
    )

    with session.begin():
        lock_batch(session, doc.batch_id)
        finish_stage(session, doc, StageName.INDEX)
        move_document(session, doc, DocumentStatus.INDEXED, {DocumentStatus.EMBEDDING})
        recompute_batch(session, doc.batch_id)
    return {"indexed": written}


@app.task(name="pagecite.index", **STAGE_TASK_OPTIONS)
def index(document_id: int, version: int) -> None:
    run_stage(get_context(), StageName.INDEX, document_id, version, handle)
