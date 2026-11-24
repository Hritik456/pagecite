"""Stage 3 - embed: chunks without an embedding -> vectors, in batches of `embed_batch_size`.

Each batch commits on its own and marks embedding_done only for the rows it embedded. A redelivered
task (worker crash mid-embed, spec flow 2) finds the remaining rows and embeds only those.
"""

from sqlalchemy import bindparam, select, update
from sqlalchemy.orm import Session

from apps.api.models import Chunk, StageName

from ..celery_app import STAGE_TASK_OPTIONS, app
from ..context import WorkerContext, get_context
from ..embedding import to_float32_bytes
from ..errors import PermanentError
from ..stages import DocRef, finish_stage, run_stage

_chunks = Chunk.__table__
SET_EMBEDDING = (
    update(_chunks)
    .where(_chunks.c.id == bindparam("chunk_id"))
    .values(embedding=bindparam("vector"), embedding_model=bindparam("model"), embedding_done=True)
)


def handle(ctx: WorkerContext, session: Session, doc: DocRef) -> dict:
    s = ctx.settings
    embedded = batches = 0
    while True:
        with session.begin():
            rows = session.execute(
                select(Chunk.id, Chunk.text)
                .where(Chunk.document_id == doc.id, Chunk.version == doc.version, Chunk.embedding_done.is_(False))
                .order_by(Chunk.ord)
                .limit(s.embed_batch_size)
            ).all()
        if not rows:
            break
        vectors = ctx.embedder.embed([r.text for r in rows])
        if len(vectors) != len(rows) or any(len(v) != s.embed_dim for v in vectors):
            raise PermanentError("EMBEDDING_SHAPE", f"expected {len(rows)} vectors of dim {s.embed_dim}")
        with session.begin():
            session.execute(
                SET_EMBEDDING,
                [
                    {"chunk_id": r.id, "vector": to_float32_bytes(v), "model": ctx.embedder.model}
                    for r, v in zip(rows, vectors)
                ],
            )
        embedded += len(rows)
        batches += 1

    with session.begin():
        finish_stage(session, doc, StageName.EMBED)
    return {"embedded": embedded, "batches": batches}


@app.task(name="pagecite.embed", **STAGE_TASK_OPTIONS)
def embed(document_id: int, version: int) -> None:
    run_stage(get_context(), StageName.EMBED, document_id, version, handle)
