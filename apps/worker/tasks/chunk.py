"""Stage 2 - chunk: document_pages -> chunks (~500 tokens, ~50 overlap, page_from/page_to)."""

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from apps.api.models import Chunk, DocumentPage, StageName

from ..celery_app import STAGE_TASK_OPTIONS, app
from ..chunking import chunk_pages
from ..context import WorkerContext, get_context
from ..errors import PermanentError
from ..parsing import Page
from ..stages import DocRef, finish_stage, run_stage

# Upsert on UNIQUE(document_id, version, ord). If a chunk's text changed (e.g. a chunker change on a
# redo), its stored embedding no longer matches, so it is cleared; the embedding_* assignments come
# first because MySQL evaluates them left to right, before `text` is overwritten.
UPSERT_CHUNK = text(
    """
    INSERT INTO chunks (document_id, tenant_id, version, ord, page_from, page_to, text, token_count, embedding_done)
    VALUES (:document_id, :tenant_id, :version, :ord, :page_from, :page_to, :text, :token_count, FALSE) AS new
    ON DUPLICATE KEY UPDATE
        embedding_done  = IF(CAST(chunks.text AS BINARY) = CAST(new.text AS BINARY), chunks.embedding_done, FALSE),
        embedding       = IF(CAST(chunks.text AS BINARY) = CAST(new.text AS BINARY), chunks.embedding, NULL),
        embedding_model = IF(CAST(chunks.text AS BINARY) = CAST(new.text AS BINARY), chunks.embedding_model, NULL),
        page_from = new.page_from,
        page_to = new.page_to,
        token_count = new.token_count,
        text = new.text
    """
)


def handle(ctx: WorkerContext, session: Session, doc: DocRef) -> dict:
    s = ctx.settings
    with session.begin():
        rows = session.execute(
            select(DocumentPage.page_no, DocumentPage.text, DocumentPage.ocr)
            .where(DocumentPage.document_id == doc.id, DocumentPage.version == doc.version)
            .order_by(DocumentPage.page_no)
        ).all()
    pages = [Page(r.page_no, r.text, r.ocr) for r in rows]
    chunks = chunk_pages(pages, target=s.chunk_tokens, overlap=s.chunk_overlap, encoding=s.tokenizer)
    if not chunks:
        raise PermanentError("NO_TEXT", "parsing produced no text to chunk")

    with session.begin():
        session.execute(
            UPSERT_CHUNK,
            [
                {
                    "document_id": doc.id, "tenant_id": doc.tenant_id, "version": doc.version, "ord": c.ord,
                    "page_from": c.page_from, "page_to": c.page_to, "text": c.text, "token_count": c.token_count,
                }
                for c in chunks
            ],
        )
        session.execute(
            delete(Chunk).where(Chunk.document_id == doc.id, Chunk.version == doc.version, Chunk.ord >= len(chunks))
        )
        finish_stage(session, doc, StageName.CHUNK)
    return {"chunks": len(chunks), "tokens": sum(c.token_count for c in chunks)}


@app.task(name="pagecite.chunk", **STAGE_TASK_OPTIONS)
def chunk(self, document_id: int, version: int) -> None:
    run_stage(get_context(), StageName.CHUNK, document_id, version, handle, task=self)
