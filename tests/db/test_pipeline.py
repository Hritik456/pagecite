"""Pipeline stages against the MySQL test DB (FakeStorage, FakeEmbedder, recorded enqueues; no Redis).

Handlers are driven through run_stage, exactly as the Celery tasks call them.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.models import StageName
from apps.worker.context import use_context
from apps.worker.relay import relay_once
from apps.worker.stages import job_key, lock_batch, recompute_batch, run_stage
from apps.worker.tasks import chunk, embed, parse
from tests.conftest import Recorder, make_worker_ctx
from tests.helpers import auth, file_spec

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
MANIFEST = json.loads((SAMPLES / "manifest.json").read_text())
SAMPLE = SAMPLES / MANIFEST[0]["file"]  # 3-page text PDF


class Crash(BaseException):
    """Simulates the worker process dying: run_stage does not catch BaseException."""


@pytest.fixture
def ctx(clean_db, test_env, fake_storage):
    ctx = make_worker_ctx(test_env, fake_storage, chunk_tokens=120, chunk_overlap=20, embed_batch_size=2)
    yield ctx
    ctx.sessions.kw["bind"].dispose()


def upload(client, fake_storage, key: str, files: list[tuple[str, bytes]]) -> list[int]:
    """P1 flow: create batch -> PUT -> complete. Returns document ids (now UPLOADED)."""
    body = client.post("/batches", json={"files": [file_spec(n, d) for n, d in files]}, headers=auth(key)).get_json()
    ids = []
    for up, (_, data) in zip(body["uploads"], files):
        assert fake_storage.put_via_url(up["presignedUrl"], data, up["headers"]) == 200
        assert client.post(f"/documents/{up['documentId']}/complete", headers=auth(key)).status_code == 200
        ids.append(up["documentId"])
    return ids


@pytest.fixture
def doc_id(client, fake_storage, make_tenant):
    _, key = make_tenant()
    return upload(client, fake_storage, key, [("policy.pdf", SAMPLE.read_bytes())])[0]


def run(ctx, stage: StageName, document_id: int, version: int = 1) -> str:
    handler = {StageName.PARSE: parse.handle, StageName.CHUNK: chunk.handle, StageName.EMBED: embed.handle}[stage]
    return run_stage(ctx, stage, document_id, version, handler)


# --- relay ------------------------------------------------------------------------------------


def test_relay_enqueues_parse_and_marks_published(client, fake_storage, make_tenant, db_scalar, ctx):
    _, key = make_tenant()
    ids = upload(client, fake_storage, key, [("a.pdf", b"%PDF a"), ("b.pdf", b"%PDF b")])
    rec = Recorder()

    with ctx.sessions() as session:
        assert relay_once(session, rec) == 2
        assert relay_once(session, rec) == 0

    assert rec.calls == [("pagecite.parse", ids[0], 1), ("pagecite.parse", ids[1], 1)]
    assert db_scalar("SELECT COUNT(*) FROM outbox_events WHERE published_at IS NULL") == 0


def test_relay_rolls_back_when_enqueue_fails(doc_id, db_scalar, ctx):
    def broken(*_args):
        raise ConnectionError("broker down")

    with ctx.sessions() as session, pytest.raises(ConnectionError):
        relay_once(session, broken)
    assert db_scalar("SELECT COUNT(*) FROM outbox_events WHERE published_at IS NULL") == 1


def test_relays_skip_rows_locked_by_another_relay(doc_id, clean_db, db_scalar, ctx):
    rec = Recorder()
    with clean_db.connect() as other:  # another relay holding the row lock mid-publish
        other.execute(text("SELECT id FROM outbox_events FOR UPDATE"))
        with ctx.sessions() as session:
            assert relay_once(session, rec) == 0  # skipped, not blocked
        other.rollback()
    with ctx.sessions() as session:
        assert relay_once(session, rec) == 1
    assert len(rec.calls) == 1


# --- stages ------------------------------------------------------------------------------------


def test_parse_chunk_embed_happy_path(doc_id, db_scalar, ctx):
    assert run(ctx, StageName.PARSE, doc_id) == "succeeded"
    assert db_scalar("SELECT COUNT(*) FROM document_pages WHERE document_id = :d", d=doc_id) == 3
    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=doc_id) == "PARSING"

    assert run(ctx, StageName.CHUNK, doc_id) == "succeeded"
    n_chunks = db_scalar("SELECT COUNT(*) FROM chunks WHERE document_id = :d", d=doc_id)
    assert n_chunks > 4
    assert db_scalar("SELECT MAX(token_count) FROM chunks") <= 120 + 5
    assert db_scalar("SELECT MIN(page_from) FROM chunks") == 1
    assert db_scalar("SELECT MAX(page_to) FROM chunks") == 3

    assert run(ctx, StageName.EMBED, doc_id) == "succeeded"
    assert db_scalar("SELECT COUNT(*) FROM chunks WHERE embedding_done") == n_chunks
    assert db_scalar("SELECT MIN(LENGTH(embedding)) FROM chunks") == 1536 * 4
    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=doc_id) == "EMBEDDING"

    assert ctx.enqueue.calls == [("pagecite.chunk", doc_id, 1), ("pagecite.embed", doc_id, 1), ("pagecite.index", doc_id, 1)]
    stages = db_scalar(
        "SELECT GROUP_CONCAT(CONCAT(stage, '=', status) ORDER BY id) FROM document_stages WHERE document_id = :d",
        d=doc_id,
    )
    assert stages == "parse=SUCCEEDED,chunk=SUCCEEDED,embed=SUCCEEDED"
    assert db_scalar("SELECT COUNT(*) FROM processed_jobs") == 3


def test_chunk_task_run_twice_gives_the_same_chunks(doc_id, db_scalar, ctx):
    run(ctx, StageName.PARSE, doc_id)
    assert run(ctx, StageName.CHUNK, doc_id) == "succeeded"
    first = db_scalar("SELECT GROUP_CONCAT(id ORDER BY ord) FROM chunks")

    # Redelivery after success: processed_jobs short-circuits, but the next stage is enqueued again
    # in case the first run died between its commit and its enqueue.
    assert run(ctx, StageName.CHUNK, doc_id) == "duplicate"
    assert db_scalar("SELECT GROUP_CONCAT(id ORDER BY ord) FROM chunks") == first
    assert ctx.enqueue.names().count("pagecite.embed") == 2


def test_chunk_redo_without_processed_job_upserts_in_place(doc_id, db_scalar, clean_db, ctx):
    """Crash after the output commit but before processed_jobs existed: the redo must not duplicate rows."""
    run(ctx, StageName.PARSE, doc_id)
    run(ctx, StageName.CHUNK, doc_id)
    before = db_scalar("SELECT GROUP_CONCAT(CONCAT(id, ':', ord, ':', token_count) ORDER BY ord) FROM chunks")
    with clean_db.begin() as conn:
        conn.execute(text("DELETE FROM processed_jobs WHERE job_key = :k"), {"k": job_key(doc_id, StageName.CHUNK, 1)})

    assert run(ctx, StageName.CHUNK, doc_id) == "succeeded"
    assert db_scalar("SELECT GROUP_CONCAT(CONCAT(id, ':', ord, ':', token_count) ORDER BY ord) FROM chunks") == before


def test_concurrent_duplicate_that_finishes_first_wins(doc_id, db_scalar, clean_db, ctx):
    """Two copies of a task: the one that loses the processed_jobs insert rolls back and reports duplicate."""

    def racing_handle(ctx_, session, doc):
        with clean_db.begin() as conn:  # the other copy commits first
            conn.execute(text("INSERT INTO processed_jobs (job_key) VALUES (:k)"), {"k": job_key(doc.id, StageName.PARSE, 1)})
        return parse.handle(ctx_, session, doc)

    assert run_stage(ctx, StageName.PARSE, doc_id, 1, racing_handle) == "duplicate"
    assert db_scalar("SELECT COUNT(*) FROM document_pages") == 0  # this copy's output was rolled back
    assert ctx.enqueue.calls == [("pagecite.chunk", doc_id, 1)]


def test_embed_redo_after_crash_embeds_only_the_remaining_rows(doc_id, db_scalar, ctx):
    run(ctx, StageName.PARSE, doc_id)
    run(ctx, StageName.CHUNK, doc_id)
    total = db_scalar("SELECT COUNT(*) FROM chunks")

    healthy = ctx.embedder

    class CrashAfterFirstBatch:
        model = healthy.model

        def __init__(self):
            self.batches = 0

        def embed(self, texts):
            self.batches += 1
            if self.batches > 1:
                raise Crash()
            return healthy.embed(texts)

    ctx.embedder = CrashAfterFirstBatch()
    with pytest.raises(Crash):
        run(ctx, StageName.EMBED, doc_id)
    assert db_scalar("SELECT COUNT(*) FROM chunks WHERE embedding_done") == 2  # batch 1 committed
    assert db_scalar("SELECT COUNT(*) FROM processed_jobs WHERE job_key LIKE '%:embed:%'") == 0

    ctx.embedder = healthy
    healthy.calls.clear()
    assert run(ctx, StageName.EMBED, doc_id) == "succeeded"  # the redelivered task
    assert sum(healthy.calls) == total - 2  # nothing embedded twice
    assert db_scalar("SELECT COUNT(*) FROM chunks WHERE embedding_done") == total
    assert db_scalar("SELECT attempts FROM document_stages WHERE stage = 'embed'") == 2


def test_permanent_error_fails_once_without_retry(client, fake_storage, make_tenant, db_scalar, ctx):
    _, key = make_tenant()
    (bad,) = upload(client, fake_storage, key, [("broken.pdf", b"%PDF-1.4 this is not really a pdf")])

    # Through the real Celery task in eager mode: a retry would show up as a second attempt or an exception.
    with use_context(ctx):
        result = parse.parse.apply(args=(bad, 1))
    assert result.successful()

    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=bad) == "FAILED"
    assert db_scalar("SELECT status FROM document_stages WHERE document_id = :d", d=bad) == "FAILED"
    assert db_scalar("SELECT attempts FROM document_stages WHERE document_id = :d", d=bad) == 1
    assert db_scalar("SELECT last_error FROM document_stages WHERE document_id = :d", d=bad).startswith("CORRUPT:")
    assert db_scalar("SELECT status FROM batches") == "PARTIAL"
    assert db_scalar("SELECT failed FROM batches") == 1
    assert ctx.enqueue.calls == []  # the pipeline stops here


def test_stale_version_and_failed_documents_are_skipped(doc_id, clean_db, ctx):
    assert run(ctx, StageName.PARSE, doc_id, version=2) == "skipped"
    with clean_db.begin() as conn:
        conn.execute(text("UPDATE documents SET status = 'FAILED' WHERE id = :d"), {"d": doc_id})
    assert run(ctx, StageName.PARSE, doc_id) == "skipped"
    assert ctx.enqueue.calls == []


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["INDEXED", "INDEXED"], ("DONE", 2, 0)),
        (["INDEXED", "FAILED"], ("PARTIAL", 1, 1)),
        (["INDEXED", "EMBEDDING"], ("PROCESSING", 1, 0)),
    ],
)
def test_batch_status_is_derived_from_documents(
    client, fake_storage, make_tenant, clean_db, db_scalar, ctx, statuses, expected
):
    _, key = make_tenant()
    ids = upload(client, fake_storage, key, [("a.pdf", b"%PDF a"), ("b.pdf", b"%PDF b")])
    batch_id = db_scalar("SELECT id FROM batches")
    with clean_db.begin() as conn:
        for doc, status in zip(ids, statuses):
            conn.execute(text("UPDATE documents SET status = :s WHERE id = :d"), {"s": status, "d": doc})
    with ctx.sessions() as session, session.begin():
        lock_batch(session, batch_id)
        recompute_batch(session, batch_id)
    assert (
        db_scalar("SELECT status FROM batches"),
        db_scalar("SELECT succeeded FROM batches"),
        db_scalar("SELECT failed FROM batches"),
    ) == expected


def test_get_batch_reports_per_stage_progress(client, fake_storage, make_tenant, ctx):
    _, key = make_tenant()
    (doc,) = upload(client, fake_storage, key, [("policy.pdf", SAMPLE.read_bytes())])
    run(ctx, StageName.PARSE, doc)

    body = client.get("/batches/" + str(client_batch_id(client, key, doc)), headers=auth(key)).get_json()
    (document,) = body["documents"]
    assert document["status"] == "PARSING"
    assert [(s["stage"], s["status"], s["attempts"]) for s in document["stages"]] == [("parse", "SUCCEEDED", 1)]


def client_batch_id(client, key: str, document_id: int) -> int:
    return client.get(f"/documents/{document_id}", headers=auth(key)).get_json()["document"]["batchId"]
