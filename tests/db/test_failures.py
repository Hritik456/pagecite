"""P3 failure handling against the MySQL test DB: retries, DLQ, attempt cap, cancel, replay, DLQ API.

Handlers run through run_stage with a FakeTask standing in for the bound Celery task (no Redis here;
the Redis side is tests/integration/test_failures.py).
"""

from types import SimpleNamespace

import httpx2
import openai
import pytest
from sqlalchemy import text

from apps.api.models import StageName
from apps.worker.stages import run_stage
from apps.worker.tasks import chunk, embed, parse
from tests.conftest import Recorder, make_worker_ctx
from tests.db.test_pipeline import SAMPLE, upload
from tests.helpers import auth


class RetryCalled(Exception):
    def __init__(self, countdown):
        super().__init__(countdown)
        self.countdown = countdown


class FakeTask:
    """The bits of a bound Celery task that run_stage uses."""

    def __init__(self, retries: int = 0, max_retries: int = 5):
        self.request = SimpleNamespace(retries=retries)
        self.max_retries = max_retries
        self.retries_requested: list[int] = []

    def retry(self, exc=None, countdown=None):
        self.retries_requested.append(countdown)
        return RetryCalled(countdown)


class Crash(BaseException):
    """A SIGKILL/OOM mid-handler: nothing after begin_stage runs."""


class RateLimitedEmbedder:
    model = "rate-limited"

    def embed(self, texts):
        req = httpx2.Request("POST", "https://api.openai.com/v1/embeddings")
        raise openai.RateLimitError(
            "429", response=httpx2.Response(429, headers={"retry-after": "2"}, request=req), body=None
        )


@pytest.fixture
def ctx(clean_db, test_env, fake_storage):
    ctx = make_worker_ctx(test_env, fake_storage, chunk_tokens=120, chunk_overlap=20, embed_batch_size=4,
                          stage_attempt_limit=3)
    yield ctx
    ctx.sessions.kw["bind"].dispose()


@pytest.fixture
def tenant_key(make_tenant):
    return make_tenant()[1]


@pytest.fixture
def doc_id(client, fake_storage, tenant_key):
    return upload(client, fake_storage, tenant_key, [("policy.pdf", SAMPLE.read_bytes())])[0]


def run(ctx, stage, document_id, version=1, task=None, handler=None):
    default = {StageName.PARSE: parse.handle, StageName.CHUNK: chunk.handle, StageName.EMBED: embed.handle}[stage]
    return run_stage(ctx, stage, document_id, version, handler or default, task=task)


def to_embed(ctx, doc_id):
    assert run(ctx, StageName.PARSE, doc_id) == "succeeded"
    assert run(ctx, StageName.CHUNK, doc_id) == "succeeded"


# --- retries ------------------------------------------------------------------------------------


def test_transient_error_with_retries_left_schedules_a_retry(doc_id, ctx, db_scalar):
    to_embed(ctx, doc_id)
    ctx.embedder = RateLimitedEmbedder()
    task = FakeTask(retries=1)

    with pytest.raises(RetryCalled) as exc:
        run(ctx, StageName.EMBED, doc_id, task=task)

    assert 2 <= exc.value.countdown <= ctx.settings.retry_backoff_max_s  # Retry-After (2 s) honoured
    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=doc_id) == "RETRY_WAIT"
    assert db_scalar("SELECT status FROM document_stages WHERE stage = 'embed'") == "RETRY_WAIT"
    assert db_scalar("SELECT last_error FROM document_stages WHERE stage = 'embed'").startswith("RATE_LIMITED")
    assert db_scalar("SELECT COUNT(*) FROM dead_letters") == 0


def test_retry_resumes_from_retry_wait(doc_id, ctx, db_scalar):
    to_embed(ctx, doc_id)
    healthy = ctx.embedder
    ctx.embedder = RateLimitedEmbedder()
    with pytest.raises(RetryCalled):
        run(ctx, StageName.EMBED, doc_id, task=FakeTask(retries=0))
    ctx.embedder = healthy
    assert run(ctx, StageName.EMBED, doc_id, task=FakeTask(retries=1)) == "succeeded"
    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=doc_id) == "EMBEDDING"
    assert db_scalar("SELECT attempts FROM document_stages WHERE stage = 'embed'") == 2


def test_exhausted_retries_go_to_the_dlq(doc_id, ctx, db_scalar):
    to_embed(ctx, doc_id)
    ctx.embedder = RateLimitedEmbedder()
    task = FakeTask(retries=5)

    assert run(ctx, StageName.EMBED, doc_id, task=task) == "failed"

    assert task.retries_requested == []
    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=doc_id) == "FAILED"
    assert db_scalar("SELECT status FROM document_stages WHERE stage = 'embed'") == "FAILED"
    assert db_scalar("SELECT kind FROM dead_letters") == "exhausted"
    assert db_scalar("SELECT error_code FROM dead_letters") == "RATE_LIMITED"
    assert db_scalar("SELECT JSON_EXTRACT(payload, '$.retries') FROM dead_letters") == "5"
    assert db_scalar("SELECT status FROM batches") == "PARTIAL"


def test_permanent_error_is_dead_lettered_without_retry(client, fake_storage, tenant_key, ctx, db_scalar):
    (bad,) = upload(client, fake_storage, tenant_key, [("broken.pdf", b"%PDF-1.4 not really")])
    task = FakeTask()
    assert run(ctx, StageName.PARSE, bad, task=task) == "failed"
    assert task.retries_requested == []
    assert (db_scalar("SELECT kind FROM dead_letters"), db_scalar("SELECT error_code FROM dead_letters")) == (
        "permanent", "CORRUPT",
    )
    assert db_scalar("SELECT attempts FROM document_stages") == 1


# --- attempt cap: a crash mid-handler still counts ----------------------------------------------


def test_crashes_count_and_the_next_delivery_goes_to_the_dlq(doc_id, ctx, db_scalar):
    calls = []

    def crashing_handler(ctx_, session, doc):
        calls.append(1)
        raise Crash()

    limit = ctx.settings.stage_attempt_limit  # 3 in this fixture
    for _ in range(limit):  # each "delivery" dies after begin_stage committed attempts + 1
        with pytest.raises(Crash):
            run(ctx, StageName.PARSE, doc_id, handler=crashing_handler)
    assert db_scalar("SELECT attempts FROM document_stages WHERE stage = 'parse'") == limit
    assert db_scalar("SELECT COUNT(*) FROM processed_jobs") == 0

    assert run(ctx, StageName.PARSE, doc_id, handler=crashing_handler) == "failed"
    assert len(calls) == limit  # the handler did not run again
    assert (db_scalar("SELECT kind FROM dead_letters"), db_scalar("SELECT error_code FROM dead_letters")) == (
        "attempt_limit", "ATTEMPT_LIMIT",
    )
    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=doc_id) == "FAILED"


# --- cancel ------------------------------------------------------------------------------------


def test_cancel_endpoint_and_tasks_honour_it(client, fake_storage, tenant_key, make_tenant, ctx, db_scalar):
    up = client.post("/batches", headers=auth(tenant_key), json={"files": [
        {"name": "never-uploaded.pdf", "size": 10, "sha256": "a" * 64, "mime": "application/pdf"},
    ]}).get_json()
    (uploaded,) = upload(client, fake_storage, tenant_key, [("policy.pdf", SAMPLE.read_bytes())])
    never = up["uploads"][0]["documentId"]
    batch_id = db_scalar("SELECT batch_id FROM documents WHERE id = :d", d=uploaded)

    _, other_key = make_tenant("other")
    assert client.post(f"/batches/{batch_id}/cancel", headers=auth(other_key)).status_code == 404

    resp = client.post(f"/batches/{batch_id}/cancel", headers=auth(tenant_key))
    assert resp.status_code == 202
    assert (resp.get_json()["cancelled"], resp.get_json()["cancelRequested"]) == (0, 1)
    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=uploaded) == "CANCEL_REQUESTED"
    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=never) == "CREATED"  # other batch

    assert run(ctx, StageName.PARSE, uploaded) == "cancelled"
    assert ctx.enqueue.calls == []
    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=uploaded) == "CANCELLED"
    assert db_scalar("SELECT status FROM document_stages WHERE document_id = :d", d=uploaded) == "CANCELLED"
    assert db_scalar("SELECT status FROM batches WHERE id = :b", b=batch_id) == "CANCELLED"

    resp = client.post(f"/batches/{up['batchId']}/cancel", headers=auth(tenant_key))
    assert resp.get_json()["cancelled"] == 1  # a not-yet-uploaded document is cancelled at once
    assert db_scalar("SELECT status FROM batches WHERE id = :b", b=up["batchId"]) == "CANCELLED"


def test_cancel_during_a_handler_stops_the_pipeline(doc_id, ctx, clean_db, db_scalar):
    assert run(ctx, StageName.PARSE, doc_id) == "succeeded"
    ctx.enqueue.calls.clear()

    def chunk_then_cancel(ctx_, session, doc):
        result = chunk.handle(ctx_, session, doc)  # output committed
        with clean_db.begin() as conn:  # the user cancels while the stage is finishing
            conn.execute(text("UPDATE documents SET status = 'CANCEL_REQUESTED' WHERE id = :d"), {"d": doc.id})
        return result

    assert run(ctx, StageName.CHUNK, doc_id, handler=chunk_then_cancel) == "cancelled"
    assert ctx.enqueue.calls == []  # embed is never enqueued
    assert db_scalar("SELECT status FROM documents WHERE id = :d", d=doc_id) == "CANCELLED"


# --- replay ------------------------------------------------------------------------------------


def test_replay_creates_a_new_version(client, fake_storage, tenant_key, make_tenant, ctx, db_scalar):
    (bad,) = upload(client, fake_storage, tenant_key, [("broken.pdf", b"%PDF-1.4 not really")])
    run(ctx, StageName.PARSE, bad)  # -> FAILED + DLQ
    _, other_key = make_tenant("other")
    assert client.post(f"/documents/{bad}/replay", headers=auth(other_key)).status_code == 404

    resp = client.post(f"/documents/{bad}/replay", headers=auth(tenant_key))

    assert resp.status_code == 202
    body = resp.get_json()
    assert body["document"]["processingVersion"] == 2 and body["document"]["status"] == "UPLOADED"
    assert body["replayedDeadLetters"] == 1
    assert db_scalar("SELECT COUNT(*) FROM dead_letters WHERE replayed_at IS NULL") == 0
    assert db_scalar("SELECT COUNT(*) FROM outbox_events WHERE published_at IS NULL") == 2  # v1 + v2
    assert db_scalar(
        "SELECT CAST(JSON_EXTRACT(payload, '$.version') AS UNSIGNED) FROM outbox_events ORDER BY id DESC LIMIT 1"
    ) == 2
    assert db_scalar("SELECT status FROM batches") == "PROCESSING"  # re-opened
    assert db_scalar("SELECT COUNT(*) FROM document_stages WHERE version = 1") == 1  # history kept


def test_replay_of_an_in_flight_document_is_409(doc_id, client, tenant_key):
    resp = client.post(f"/documents/{doc_id}/replay", headers=auth(tenant_key))  # UPLOADED
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "NOT_REPLAYABLE"


def test_stale_version_task_after_replay_is_skipped(client, fake_storage, tenant_key, ctx):
    (bad,) = upload(client, fake_storage, tenant_key, [("broken.pdf", b"%PDF-1.4 not really")])
    run(ctx, StageName.PARSE, bad)
    client.post(f"/documents/{bad}/replay", headers=auth(tenant_key))
    assert run(ctx, StageName.PARSE, bad, version=1) == "skipped"  # a late v1 message does nothing


# --- DLQ API -----------------------------------------------------------------------------------


def test_dead_letters_list_is_tenant_scoped(client, fake_storage, tenant_key, make_tenant, ctx):
    (bad,) = upload(client, fake_storage, tenant_key, [("broken.pdf", b"%PDF-1.4 not really")])
    run(ctx, StageName.PARSE, bad)
    _, other_key = make_tenant("other")

    mine = client.get("/dead-letters", headers=auth(tenant_key)).get_json()["deadLetters"]
    assert [(d["documentId"], d["kind"], d["errorCode"], d["filename"]) for d in mine] == [
        (bad, "permanent", "CORRUPT", "broken.pdf")
    ]
    assert client.get("/dead-letters", headers=auth(other_key)).get_json()["deadLetters"] == []

    client.post(f"/documents/{bad}/replay", headers=auth(tenant_key))
    assert client.get("/dead-letters", headers=auth(tenant_key)).get_json()["deadLetters"] == []
    assert len(client.get("/dead-letters?all=1", headers=auth(tenant_key)).get_json()["deadLetters"]) == 1
