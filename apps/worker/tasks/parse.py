"""Stage 1 - parse: S3 object -> document_pages (text per page, OCR for scanned pages)."""

from sqlalchemy import delete
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from apps.api.models import DocumentPage, StageName

from ..celery_app import STAGE_TASK_OPTIONS, app
from ..context import WorkerContext, get_context
from ..parsing import parse_document
from ..stages import DocRef, finish_stage, run_stage


def handle(ctx: WorkerContext, session: Session, doc: DocRef) -> dict:
    data = ctx.storage.get(doc.object_key)
    pages = parse_document(data, doc.mime, ctx.ocr, ctx.ocr_limits, clock=ctx.clock)

    with session.begin():
        stmt = insert(DocumentPage).values(
            [
                {"document_id": doc.id, "version": doc.version, "page_no": p.page_no, "text": p.text, "ocr": p.ocr}
                for p in pages
            ]
        )
        session.execute(stmt.on_duplicate_key_update(text=stmt.inserted.text, ocr=stmt.inserted.ocr))
        session.execute(
            delete(DocumentPage).where(
                DocumentPage.document_id == doc.id,
                DocumentPage.version == doc.version,
                DocumentPage.page_no > len(pages),
            )
        )
        finish_stage(session, doc, StageName.PARSE)
    return {"pages": len(pages), "ocr_pages": sum(p.ocr for p in pages), "bytes": len(data)}


@app.task(name="pagecite.parse", **STAGE_TASK_OPTIONS)
def parse(document_id: int, version: int) -> None:
    run_stage(get_context(), StageName.PARSE, document_id, version, handle)
