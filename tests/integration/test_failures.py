"""P3 against real MySQL/MinIO/Redis with the real (eager) Celery tasks: retries, DLQ, replay as a new
version, the atomic version swap for retrieval, orphan keys, cancel mid-pipeline."""

import httpx2
import openai
import pytest
from sqlalchemy import text

from apps.api.retrieval.search import Hit
from apps.api.retrieval.pipeline import RetrievalDeps, load_candidates
from apps.api.search_index import rebuild
from apps.worker.embedding import FakeEmbedder, FaultyEmbedder
from apps.worker.errors import PermanentError
from apps.worker.relay import relay_once
from apps.worker.tasks import chunk as chunk_task
from apps.worker.tasks import index as index_task
from tests.conftest import make_test_app
from tests.fakes import FakeLLM
from tests.helpers import auth
from tests.integration.conftest import SAMPLES, upload_files

pytestmark = pytest.mark.integration

POLICY = (SAMPLES / "01-leave-policy.pdf").read_bytes()


class FlakyEmbedder:
    """Real 429s (with Retry-After) for the first `failures` calls, then the real fake vectors."""

    def __init__(self, inner, failures: int):
        self.inner, self.failures, self.calls = inner, failures, 0
        self.model = inner.model

    def embed(self, texts):
        self.calls += 1
        if self.calls <= self.failures:
            req = httpx2.Request("POST", "https://api.openai.com/v1/embeddings")
            raise openai.RateLimitError(
                "429", response=httpx2.Response(429, headers={"retry-after": "1"}, request=req), body=None
            )
        return self.inner.embed(texts)


@pytest.fixture(autouse=True)
def eager_retries(eager):
    """With task_eager_propagates=True Celery re-raises Retry in eager mode; with False, apply() re-runs
    the task synchronously (Celery's eager retry path), so the real retry code runs here. Failures are
    still asserted on the database, not on exceptions."""
    from apps.worker.celery_app import app as celery

    celery.conf.task_eager_propagates = False
    yield


@pytest.fixture
def tenant(make_tenant):
    return make_tenant(max_file_mb=5, max_batch_files=20)


def ingest(s3_app, worker_ctx, key: str, files: list[tuple[str, bytes]]) -> dict[str, int]:
    ids = upload_files(s3_app.test_client(), key, files)
    pump(worker_ctx)
    return ids


def pump(worker_ctx) -> None:
    """Relay every pending outbox event; eager Celery runs each pipeline inline."""
    with worker_ctx.sessions() as session:
        while relay_once(session, worker_ctx.enqueue):
            pass


def doc_keys(redis_index, document_id: int, db_scalar) -> dict[int, set[int]]:
    """version -> chunk ids that currently have a Redis key, for one document."""
    r, cfg = redis_index
    ids = {int(k.decode().rsplit(":", 1)[1]) for k in r.scan_iter(match=f"{cfg.prefix}*", count=1000)}
    out: dict[int, set[int]] = {}
    for cid in ids:
        row = db_scalar("SELECT CONCAT(document_id, ':', version) FROM chunks WHERE id = :c", c=cid)
        d, v = map(int, row.split(":"))
        if d == document_id:
            out.setdefault(v, set()).add(cid)
    return out


def served_versions(worker_ctx, redis_index, tenant_id: int, document_id: int, db_scalar) -> set[int]:
    """What retrieval would serve right now, given EVERY key of the document as a candidate."""
    hits = [
        Hit(chunk_id=cid, tenant_id=tenant_id, document_id=document_id, page=1, score=1.0)
        for ids in doc_keys(redis_index, document_id, db_scalar).values()
        for cid in ids
    ]
    cands = load_candidates(worker_ctx.sessions, tenant_id, hits)
    return {db_scalar("SELECT version FROM chunks WHERE id = :c", c=c.chunk_id) for c in cands}


# --- retries ------------------------------------------------------------------------------------


def test_mocked_429_twice_then_success_is_retried(s3_app, worker_ctx, tenant, db_scalar):
    _tid, key = tenant
    worker_ctx.embedder = FlakyEmbedder(FakeEmbedder(dim=1536), failures=2)
    (doc,) = ingest(s3_app, worker_ctx, key, [("policy.pdf", POLICY)]).values()

    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=doc) == "INDEXED"
    assert db_scalar("SELECT attempts FROM document_stages WHERE stage = 'embed'") == 3
    assert worker_ctx.embedder.calls >= 3
    assert db_scalar("SELECT COUNT(*) FROM dead_letters") == 0


def test_permanent_error_is_not_retried(s3_app, worker_ctx, tenant, db_scalar):
    _tid, key = tenant
    (doc,) = ingest(s3_app, worker_ctx, key, [("broken.pdf", b"%PDF-1.4 not a real pdf")]).values()
    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=doc) == "FAILED"
    assert db_scalar("SELECT attempts FROM document_stages WHERE stage = 'parse'") == 1
    assert db_scalar("SELECT kind FROM dead_letters") == "permanent"


# --- DLQ -> replay -> version 2 -----------------------------------------------------------------


def test_exhausted_retries_land_in_the_dlq_and_replay_ends_indexed_as_v2(
    s3_app, worker_ctx, tenant, redis_index, db_scalar
):
    tid, key = tenant
    worker_ctx.embedder = FaultyEmbedder(FakeEmbedder(dim=1536), "fail-")
    (doc,) = ingest(s3_app, worker_ctx, key, [("fail-policy.pdf", POLICY)]).values()

    # v1: 1 try + 5 retries, all 429 -> DLQ
    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=doc) == "FAILED"
    assert db_scalar("SELECT attempts FROM document_stages WHERE stage = 'embed' AND version = 1") == 6
    assert (db_scalar("SELECT kind FROM dead_letters"), db_scalar("SELECT error_code FROM dead_letters")) == (
        "exhausted", "RATE_LIMITED",
    )
    assert db_scalar("SELECT active_version FROM documents WHERE id = :d", d=doc) is None
    assert doc_keys(redis_index, doc, db_scalar) == {}

    resp = s3_app.test_client().post(f"/documents/{doc}/replay", headers=auth(key))
    assert resp.status_code == 202
    pump(worker_ctx)  # v2 is not faulted

    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=doc) == "INDEXED"
    assert db_scalar("SELECT processing_version FROM documents WHERE id = :d", d=doc) == 2
    assert db_scalar("SELECT active_version FROM documents WHERE id = :d", d=doc) == 2
    assert set(doc_keys(redis_index, doc, db_scalar)) == {2}  # only v2 in the index
    assert db_scalar("SELECT COUNT(*) FROM chunks WHERE document_id = :d AND version = 1", d=doc) > 0  # history
    assert db_scalar("SELECT replayed_at IS NOT NULL FROM dead_letters") == 1
    assert served_versions(worker_ctx, redis_index, tid, doc, db_scalar) == {2}


# --- the atomic swap ----------------------------------------------------------------------------


def test_replay_of_an_indexed_document_never_serves_both_versions(
    s3_app, worker_ctx, tenant, redis_index, db_scalar, monkeypatch
):
    tid, key = tenant
    (doc,) = ingest(s3_app, worker_ctx, key, [("policy.pdf", POLICY)]).values()
    assert served_versions(worker_ctx, redis_index, tid, doc, db_scalar) == {1}

    seen: dict[str, object] = {}
    real_write, real_cleanup = index_task.write_chunks, index_task.cleanup_old_versions

    def write_then_look(*args, **kwargs):
        written = real_write(*args, **kwargs)
        seen["keys_mid"] = set(doc_keys(redis_index, doc, db_scalar))  # both versions are in Redis now
        seen["served_mid"] = served_versions(worker_ctx, redis_index, tid, doc, db_scalar)
        return written

    def look_then_cleanup(ctx, session, ref):
        seen["served_after_commit"] = served_versions(worker_ctx, redis_index, tid, doc, db_scalar)
        real_cleanup(ctx, session, ref)

    monkeypatch.setattr(index_task, "write_chunks", write_then_look)
    monkeypatch.setattr(index_task, "cleanup_old_versions", look_then_cleanup)

    # reindex while v2 is still in flight keeps the active v1
    s3_app.test_client().post(f"/documents/{doc}/replay", headers=auth(key))
    with worker_ctx.sessions() as session:
        rebuild(session, *redis_index, tid)
    assert set(doc_keys(redis_index, doc, db_scalar)) == {1}

    pump(worker_ctx)

    assert seen["keys_mid"] == {1, 2}
    assert seen["served_mid"] == {1}  # v2 keys exist, but v1 is still the one served
    assert seen["served_after_commit"] == {2}  # one commit later, only v2; never both
    assert set(doc_keys(redis_index, doc, db_scalar)) == {2}


# --- orphan keys --------------------------------------------------------------------------------


@pytest.fixture
def ask_app(test_env, s3_storage, redis_index):
    r, cfg = redis_index
    llm = FakeLLM(answer="The leave policy applies [1].")

    def deps(state):
        return RetrievalDeps(settings=state.settings, sessions=state.sessions, redis=r, index=cfg,
                             embedder=FakeEmbedder(dim=cfg.dim), llm=llm)

    app = make_test_app(test_env, s3_storage, retrieval=deps, s3_bucket=s3_storage.bucket)
    yield app
    app.extensions["pagecite"].engine.dispose()


@pytest.mark.parametrize("how", ["fails", "cancelled"])
def test_v2_ending_after_its_hset_leaves_no_orphan_keys(
    how, s3_app, worker_ctx, tenant, redis_index, db_scalar, clean_db, ask_app, monkeypatch
):
    tid, key = tenant
    (doc,) = ingest(s3_app, worker_ctx, key, [("policy.pdf", POLICY)]).values()
    v1_keys = doc_keys(redis_index, doc, db_scalar)[1]
    real_write = index_task.write_chunks

    def write_then_break(*args, **kwargs):
        written = real_write(*args, **kwargs)  # v2 keys are in Redis
        if how == "fails":
            raise PermanentError("INJECTED", "index failed after its HSET")
        with clean_db.begin() as conn:  # the user cancels the batch at this moment
            conn.execute(text("UPDATE documents SET status = 'CANCEL_REQUESTED' WHERE id = :d"), {"d": doc})
        return written

    monkeypatch.setattr(index_task, "write_chunks", write_then_break)
    s3_app.test_client().post(f"/documents/{doc}/replay", headers=auth(key))
    pump(worker_ctx)

    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=doc) == (
        "FAILED" if how == "fails" else "CANCELLED"
    )
    assert db_scalar("SELECT active_version FROM documents WHERE id = :d", d=doc) == 1
    keys = doc_keys(redis_index, doc, db_scalar)
    assert 2 not in keys  # 0 orphan keys for v2
    assert keys[1] == v1_keys  # v1 untouched
    if how == "fails":
        assert db_scalar("SELECT error_code FROM dead_letters") == "INJECTED"

    body = ask_app.test_client().post(
        "/ask", json={"question": "What does the leave policy say?", "stream": False}, headers=auth(key)
    ).get_json()
    assert body["citations"] and {c["document_id"] for c in body["citations"]} == {doc}  # v1 still answers
    assert served_versions(worker_ctx, redis_index, tid, doc, db_scalar) == {1}


# --- cancel mid-pipeline ------------------------------------------------------------------------


def test_cancel_mid_pipeline(s3_app, worker_ctx, tenant, redis_index, db_scalar, monkeypatch):
    _tid, key = tenant
    client = s3_app.test_client()
    real_chunk = chunk_task.handle

    def chunk_then_user_cancels(ctx, session, ref):
        result = real_chunk(ctx, session, ref)
        resp = client.post(f"/batches/{ref.batch_id}/cancel", headers=auth(key))
        assert resp.status_code == 202 and resp.get_json()["cancelRequested"] == 1
        return result

    monkeypatch.setattr(chunk_task, "handle", chunk_then_user_cancels)
    (doc,) = ingest(s3_app, worker_ctx, key, [("policy.pdf", POLICY)]).values()

    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=doc) == "CANCELLED"
    assert db_scalar("SELECT COUNT(*) FROM document_stages WHERE stage = 'embed'") == 0  # never enqueued
    assert db_scalar("SELECT status FROM batches") == "CANCELLED"
    assert doc_keys(redis_index, doc, db_scalar) == {}
