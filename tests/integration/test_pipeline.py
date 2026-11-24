"""The whole P2 pipeline in-process: real MinIO, MySQL test DB and Redis (test index), Celery eager.

upload (API -> MinIO) -> relay -> parse -> chunk -> embed (FakeEmbedder) -> index -> FT.SEARCH.
OCR uses the real Tesseract when it is installed, otherwise a stub that returns the scanned page's text.
"""

import json
import shutil
from pathlib import Path

import pytest
import requests
from sqlalchemy import text

from apps.worker.celery_app import app as celery
from apps.worker.context import celery_enqueue, tesseract_ocr, use_context
from apps.worker.relay import relay_once
from apps.api.search_index import (
    count_tenant,
    delete_all_keys,
    delete_tenant_keys,
    drop_index,
    fingerprint,
    rebuild,
    search_phrase,
)
from tests.conftest import make_worker_ctx
from tests.helpers import auth, file_spec

pytestmark = pytest.mark.integration

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
MANIFEST = json.loads((SAMPLES / "manifest.json").read_text())
HAS_TESSERACT = shutil.which("tesseract") is not None


def stub_ocr(image, timeout):
    scanned = next(m for m in MANIFEST if m["scanned"])
    return f"OFFICE NOTICE This is the {scanned['phrase']} for the current year."


@pytest.fixture
def eager():
    celery.conf.task_always_eager = True
    celery.conf.task_eager_propagates = True
    yield
    celery.conf.task_always_eager = False
    celery.conf.task_eager_propagates = False


@pytest.fixture
def worker_ctx(test_env, s3_storage, redis_index, eager):
    r, _cfg = redis_index
    ctx = make_worker_ctx(
        test_env, s3_storage, redis=r, ocr=tesseract_ocr if HAS_TESSERACT else stub_ocr, enqueue=celery_enqueue,
        s3_bucket=s3_storage.bucket,
    )
    with use_context(ctx):
        yield ctx
    ctx.sessions.kw["bind"].dispose()


def upload_samples(client, key: str) -> dict[str, int]:
    files = [(m["file"], (SAMPLES / m["file"]).read_bytes()) for m in MANIFEST]
    resp = client.post("/batches", json={"files": [file_spec(n, d) for n, d in files]}, headers=auth(key))
    assert resp.status_code == 201, resp.get_json()
    ids = {}
    for up, (name, data) in zip(resp.get_json()["uploads"], files):
        assert requests.put(up["presignedUrl"], data=data, headers=up["headers"], timeout=30).status_code == 200
        assert client.post(f"/documents/{up['documentId']}/complete", headers=auth(key)).status_code == 200
        ids[name] = up["documentId"]
    return ids


@pytest.fixture
def ingested(s3_app, make_tenant, worker_ctx):
    """5 samples uploaded and pushed through the relay; eager Celery runs every stage inline."""
    tenant_id, key = make_tenant(max_file_mb=5, max_batch_files=20)
    ids = upload_samples(s3_app.test_client(), key)
    with worker_ctx.sessions() as session:
        assert relay_once(session, worker_ctx.enqueue) == len(MANIFEST)
    return tenant_id, ids


def test_five_samples_end_indexed(ingested, db_scalar):
    _tenant_id, ids = ingested
    statuses = dict(
        (row.split("=")[0], row.split("=")[1])
        for row in db_scalar("SELECT GROUP_CONCAT(CONCAT(id, '=', status)) FROM documents").split(",")
    )
    assert set(statuses.values()) == {"INDEXED"}, statuses
    assert db_scalar("SELECT status FROM batches") == "DONE"
    assert db_scalar("SELECT succeeded FROM batches") == len(MANIFEST)
    scanned_id = ids[next(m["file"] for m in MANIFEST if m["scanned"])]
    assert db_scalar("SELECT ocr FROM document_pages WHERE document_id = :d", d=scanned_id) == 1


def test_ft_search_finds_each_known_phrase_in_the_right_document(ingested, redis_index, db_scalar):
    tenant_id, ids = ingested
    r, cfg = redis_index
    for item in MANIFEST:
        hits = search_phrase(r, cfg, tenant_id, item["phrase"])
        assert set(hits) == {ids[item["file"]]}, (item["file"], hits)
        # and the chunk that holds it cites the right page
        page_ok = db_scalar(
            "SELECT COUNT(*) FROM chunks WHERE document_id = :d AND REPLACE(text, '\n', ' ') LIKE :p "
            "AND page_from <= :page AND :page <= page_to",
            d=ids[item["file"]], p=f"%{item['phrase']}%", page=item["page"],
        )
        assert page_ok >= 1, item


def test_phrase_search_is_tenant_scoped(ingested, redis_index):
    tenant_id, _ids = ingested
    r, cfg = redis_index
    assert search_phrase(r, cfg, tenant_id + 1000, MANIFEST[0]["phrase"]) == []


def test_chunk_counts_match_between_mysql_and_redis(ingested, redis_index, db_scalar):
    tenant_id, _ids = ingested
    r, cfg = redis_index
    mysql_count = db_scalar(
        "SELECT COUNT(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE c.tenant_id = :t AND c.version = d.processing_version",
        t=tenant_id,
    )
    assert mysql_count > len(MANIFEST)
    assert count_tenant(r, cfg, tenant_id) == mysql_count
    assert fingerprint(r, cfg, tenant_id).keys == mysql_count


def test_reindex_one_tenant_restores_an_identical_index(ingested, redis_index, worker_ctx):
    tenant_id, _ids = ingested
    r, cfg = redis_index
    before = fingerprint(r, cfg, tenant_id)

    delete_tenant_keys(r, cfg, tenant_id)
    assert count_tenant(r, cfg, tenant_id) == 0
    with worker_ctx.sessions() as session:
        written = rebuild(session, r, cfg, tenant_id)

    assert written == {tenant_id: before.keys}
    assert fingerprint(r, cfg, tenant_id) == before


def test_flush_then_reindex_all_restores_every_tenant(ingested, redis_index, worker_ctx, s3_app, clean_db):
    tenant_id, _ids = ingested
    r, cfg = redis_index
    # A second tenant's chunks, so "all tenants" means something. Copy tenant 1's indexed rows in MySQL.
    other = tenant_id + 1
    with clean_db.begin() as conn:
        conn.execute(text("INSERT INTO tenants (id, name, api_key_hash, max_file_mb, max_batch_files) "
                          "VALUES (:id, 'other', REPEAT('b', 64), 5, 5)"), {"id": other})
    with worker_ctx.sessions() as session, session.begin():
        session.execute(text("UPDATE chunks SET tenant_id = :o WHERE document_id = (SELECT MIN(id) FROM documents)"),
                        {"o": other})
        session.execute(text("UPDATE documents SET tenant_id = :o WHERE id = (SELECT * FROM (SELECT MIN(id) FROM documents) m)"),
                        {"o": other})
    with worker_ctx.sessions() as session:
        rebuild(session, r, cfg, None)
    before = fingerprint(r, cfg)
    assert set(before.per_tenant) == {tenant_id, other}

    # "FLUSHDB", limited to the test index so the dev index on the same Redis is untouched.
    drop_index(r, cfg)
    delete_all_keys(r, cfg)

    result = s3_app.test_cli_runner().invoke(args=["reindex", "--all"])
    assert result.exit_code == 0, result.output
    assert f"total={before.keys}" in result.output
    assert fingerprint(r, cfg) == before


def test_reindex_cli_requires_exactly_one_target(s3_app, redis_index):
    runner = s3_app.test_cli_runner()
    assert runner.invoke(args=["reindex"]).exit_code != 0
    assert runner.invoke(args=["reindex", "--all", "--tenant", "1"]).exit_code != 0


def test_redelivered_index_task_is_a_noop(ingested, redis_index, worker_ctx, db_scalar):
    tenant_id, ids = ingested
    r, cfg = redis_index
    before = fingerprint(r, cfg, tenant_id)
    from apps.worker.tasks.index import index

    for doc_id in ids.values():
        index.apply(args=(doc_id, 1))  # e.g. redelivery after a worker crash
    assert fingerprint(r, cfg, tenant_id) == before
    assert db_scalar("SELECT status FROM batches") == "DONE"
