"""API tests against the compose MySQL (pagecite_test) with FakeStorage in place of S3."""

import threading

import pytest
from sqlalchemy import text

from apps.api.auth import hash_key
from tests.conftest import make_test_app
from tests.fakes import FakeStorage
from tests.helpers import DOCX_MIME, auth, file_spec, pdf_bytes

MB = 1024 * 1024


def create_batch(client, key, files):
    return client.post("/batches", json={"files": files}, headers=auth(key))


def upload(fake: FakeStorage, upload_entry: dict, data: bytes) -> int:
    return fake.put_via_url(upload_entry["presignedUrl"], data, upload_entry["headers"])


def complete(client, key, document_id):
    return client.post(f"/documents/{document_id}/complete", headers=auth(key))


def assert_envelope(resp, status: int, code: str):
    assert resp.status_code == status, resp.get_json()
    err = resp.get_json()["error"]
    assert err["code"] == code
    assert err["message"]
    assert err["request_id"] == resp.headers["X-Request-ID"]


# --- auth + envelope -------------------------------------------------------------------------


def test_health_needs_no_key(client):
    assert client.get("/health").get_json() == {"status": "ok", "sha": "dev"}


def test_health_reports_the_build_sha(test_env, fake_storage):
    app = make_test_app(test_env, fake_storage, git_sha="25decd146181764b228351849781c5f6a8ea3f79")
    assert app.test_client().get("/health").get_json()["sha"] == "25decd146181764b228351849781c5f6a8ea3f79"
    app.extensions["pagecite"].engine.dispose()


def test_missing_key_is_401(client):
    assert_envelope(client.post("/batches", json={"files": []}), 401, "UNAUTHORIZED")


def test_invalid_key_is_401(client, make_tenant):
    make_tenant()
    resp = client.get("/batches/1", headers=auth("pc_not-a-real-key"))
    assert_envelope(resp, 401, "UNAUTHORIZED")


def test_unknown_route_uses_envelope(client):
    assert_envelope(client.get("/nope"), 404, "NOT_FOUND")


def test_request_id_is_echoed(client):
    resp = client.get("/health", headers={"X-Request-ID": "req-12345678"})
    assert resp.headers["X-Request-ID"] == "req-12345678"


def test_invalid_body_is_422(client, make_tenant):
    _, key = make_tenant()
    resp = create_batch(client, key, [{"name": "a.pdf", "size": 10, "sha256": "xyz", "mime": "application/pdf"}])
    assert_envelope(resp, 422, "VALIDATION_ERROR")
    assert "sha256" in resp.get_json()["error"]["message"]


def test_api_key_is_stored_hashed(make_tenant, db_scalar):
    _, key = make_tenant()
    assert db_scalar("SELECT api_key_hash FROM tenants") == hash_key(key)
    assert key not in db_scalar("SELECT api_key_hash FROM tenants")


# --- POST /batches -----------------------------------------------------------------------------


def test_create_batch_returns_presigned_uploads(client, make_tenant, fake_storage, db_scalar):
    tenant_id, key = make_tenant()
    a, b = pdf_bytes("a"), pdf_bytes("b")
    resp = create_batch(client, key, [file_spec("A report.pdf", a), file_spec("memo.docx", b, DOCX_MIME)])

    assert resp.status_code == 201
    body = resp.get_json()
    batch_id, uploads = body["batchId"], body["uploads"]
    assert len(uploads) == 2
    first = uploads[0]
    assert first["headers"]["Content-Type"] == "application/pdf"
    assert first["headers"]["x-amz-checksum-sha256"]

    key0 = db_scalar("SELECT object_key FROM documents WHERE id = :id", id=first["documentId"])
    assert key0 == f"tenant/{tenant_id}/batch/{batch_id}/{first['documentId']}/A_report.pdf"
    assert db_scalar("SELECT COUNT(*) FROM documents WHERE status = 'CREATED'") == 2
    assert db_scalar("SELECT status FROM batches WHERE id = :id", id=batch_id) == "UPLOADING"
    assert db_scalar("SELECT COUNT(*) FROM documents WHERE object_key LIKE 'pending/%'") == 0


def test_file_too_large_is_rejected_before_any_upload(client, make_tenant, fake_storage, db_scalar):
    _, key = make_tenant(max_file_mb=1)
    big = {"name": "big.pdf", "size": 1 * MB + 1, "sha256": "a" * 64, "mime": "application/pdf"}
    resp = create_batch(client, key, [file_spec("ok.pdf", pdf_bytes()), big])

    assert_envelope(resp, 413, "QUOTA_EXCEEDED")
    assert fake_storage.presigned == {}  # no upload URL was issued
    assert db_scalar("SELECT COUNT(*) FROM batches") == 0
    assert db_scalar("SELECT COUNT(*) FROM documents") == 0


def test_too_many_files_is_rejected(client, make_tenant, db_scalar):
    _, key = make_tenant(max_batch_files=2)
    files = [file_spec(f"{i}.pdf", pdf_bytes(str(i))) for i in range(3)]
    assert_envelope(create_batch(client, key, files), 413, "QUOTA_EXCEEDED")
    assert db_scalar("SELECT COUNT(*) FROM documents") == 0


@pytest.mark.parametrize(("name", "mime"), [("a.txt", "text/plain"), ("a.pdf", DOCX_MIME)])
def test_unsupported_type_is_rejected(client, make_tenant, db_scalar, name, mime):
    _, key = make_tenant()
    assert_envelope(create_batch(client, key, [file_spec(name, b"x", mime)]), 415, "UNSUPPORTED_TYPE")
    assert db_scalar("SELECT COUNT(*) FROM documents") == 0


# --- POST /documents/:id/complete --------------------------------------------------------------


def test_complete_moves_to_uploaded_with_one_outbox_row(client, make_tenant, fake_storage, db_scalar):
    _, key = make_tenant()
    data = pdf_bytes()
    up = create_batch(client, key, [file_spec("a.pdf", data)]).get_json()["uploads"][0]
    assert upload(fake_storage, up, data) == 200

    resp = complete(client, key, up["documentId"])

    assert resp.status_code == 200
    assert resp.get_json()["document"]["status"] == "UPLOADED"
    assert db_scalar("SELECT COUNT(*) FROM outbox_events") == 1
    assert db_scalar("SELECT type FROM outbox_events") == "document.uploaded"
    assert db_scalar("SELECT aggregate_id FROM outbox_events") == up["documentId"]
    assert db_scalar("SELECT CAST(JSON_EXTRACT(payload, '$.version') AS UNSIGNED) FROM outbox_events") == 1


def test_duplicate_complete_is_a_noop(client, make_tenant, fake_storage, db_scalar):
    _, key = make_tenant()
    data = pdf_bytes()
    up = create_batch(client, key, [file_spec("a.pdf", data)]).get_json()["uploads"][0]
    upload(fake_storage, up, data)

    first = complete(client, key, up["documentId"])
    second = complete(client, key, up["documentId"])

    assert first.status_code == second.status_code == 200
    assert second.get_json()["document"]["status"] == "UPLOADED"
    assert db_scalar("SELECT COUNT(*) FROM outbox_events") == 1


def test_concurrent_duplicate_completes_write_one_outbox_row(app, make_tenant, fake_storage, db_scalar):
    _, key = make_tenant()
    data = pdf_bytes()
    up = create_batch(app.test_client(), key, [file_spec("a.pdf", data)]).get_json()["uploads"][0]
    upload(fake_storage, up, data)

    statuses = run_concurrently(app, key, [up["documentId"]] * 8)

    assert statuses == [200] * 8
    assert db_scalar("SELECT COUNT(*) FROM outbox_events") == 1


def test_batch_becomes_processing_when_every_document_is_uploaded(client, make_tenant, fake_storage, db_scalar):
    _, key = make_tenant()
    blobs = [pdf_bytes("a"), pdf_bytes("b")]
    body = create_batch(client, key, [file_spec(f"{i}.pdf", d) for i, d in enumerate(blobs)]).get_json()
    for up, data in zip(body["uploads"], blobs):
        upload(fake_storage, up, data)

    complete(client, key, body["uploads"][0]["documentId"])
    assert db_scalar("SELECT status FROM batches") == "UPLOADING"
    complete(client, key, body["uploads"][1]["documentId"])
    assert db_scalar("SELECT status FROM batches") == "PROCESSING"


def test_concurrent_completes_in_one_batch(app, make_tenant, fake_storage, db_scalar):
    """Parallel completes of different documents: no deadlock, and the batch ends PROCESSING."""
    _, key = make_tenant(max_batch_files=10)
    blobs = [pdf_bytes(str(i)) for i in range(6)]
    body = create_batch(app.test_client(), key, [file_spec(f"{i}.pdf", d) for i, d in enumerate(blobs)]).get_json()
    for up, data in zip(body["uploads"], blobs):
        upload(fake_storage, up, data)

    statuses = run_concurrently(app, key, [u["documentId"] for u in body["uploads"]])

    assert statuses == [200] * 6
    assert db_scalar("SELECT COUNT(*) FROM documents WHERE status = 'UPLOADED'") == 6
    assert db_scalar("SELECT COUNT(*) FROM outbox_events") == 6
    assert db_scalar("SELECT status FROM batches") == "PROCESSING"


def test_complete_before_upload_is_409(client, make_tenant, db_scalar):
    _, key = make_tenant()
    up = create_batch(client, key, [file_spec("a.pdf", pdf_bytes())]).get_json()["uploads"][0]
    assert_envelope(complete(client, key, up["documentId"]), 409, "NOT_UPLOADED")
    assert db_scalar("SELECT status FROM documents") == "CREATED"
    assert db_scalar("SELECT COUNT(*) FROM outbox_events") == 0


def test_checksum_mismatch_deletes_object_and_keeps_created(client, make_tenant, fake_storage, db_scalar):
    _, key = make_tenant()
    data = pdf_bytes("real")
    up = create_batch(client, key, [file_spec("a.pdf", data)]).get_json()["uploads"][0]
    object_key = db_scalar("SELECT object_key FROM documents")
    fake_storage.put_raw(object_key, pdf_bytes("evil", size=len(data)))  # same size, other bytes

    assert_envelope(complete(client, key, up["documentId"]), 422, "CHECKSUM_MISMATCH")
    assert object_key in fake_storage.deleted
    assert db_scalar("SELECT status FROM documents") == "CREATED"
    assert db_scalar("SELECT COUNT(*) FROM outbox_events") == 0


def test_size_mismatch_deletes_object(client, make_tenant, fake_storage, db_scalar):
    _, key = make_tenant()
    data = pdf_bytes()
    up = create_batch(client, key, [file_spec("a.pdf", data)]).get_json()["uploads"][0]
    object_key = db_scalar("SELECT object_key FROM documents")
    fake_storage.put_raw(object_key, data + b"extra")

    assert_envelope(complete(client, key, up["documentId"]), 422, "SIZE_MISMATCH")
    assert object_key in fake_storage.deleted
    assert db_scalar("SELECT COUNT(*) FROM outbox_events") == 0


def test_other_tenant_cannot_complete(client, make_tenant, fake_storage, db_scalar):
    _, key_a = make_tenant("tenant-a")
    _, key_b = make_tenant("tenant-b")
    data = pdf_bytes()
    up = create_batch(client, key_a, [file_spec("a.pdf", data)]).get_json()["uploads"][0]
    upload(fake_storage, up, data)

    assert_envelope(complete(client, key_b, up["documentId"]), 404, "NOT_FOUND")
    assert db_scalar("SELECT status FROM documents") == "CREATED"
    assert db_scalar("SELECT COUNT(*) FROM outbox_events") == 0


def test_tenant_prefix_mismatch_is_rejected(client, make_tenant, fake_storage, clean_db, db_scalar):
    tenant_id, key = make_tenant()
    data = pdf_bytes()
    up = create_batch(client, key, [file_spec("a.pdf", data)]).get_json()["uploads"][0]
    upload(fake_storage, up, data)
    # A row whose key points into another tenant's prefix must never be accepted.
    with clean_db.begin() as conn:
        conn.execute(
            text("UPDATE documents SET object_key = CONCAT('tenant/999/', object_key) WHERE id = :id"),
            {"id": up["documentId"]},
        )

    assert_envelope(complete(client, key, up["documentId"]), 409, "OBJECT_KEY_MISMATCH")
    assert db_scalar("SELECT COUNT(*) FROM outbox_events") == 0


# --- GET routes --------------------------------------------------------------------------------


def test_get_batch_and_document(client, make_tenant, fake_storage):
    _, key = make_tenant()
    data = pdf_bytes()
    body = create_batch(client, key, [file_spec("a.pdf", data)]).get_json()
    up = body["uploads"][0]
    upload(fake_storage, up, data)
    complete(client, key, up["documentId"])

    batch = client.get(f"/batches/{body['batchId']}", headers=auth(key)).get_json()
    assert batch["batch"]["status"] == "PROCESSING"
    assert [d["status"] for d in batch["documents"]] == ["UPLOADED"]

    doc = client.get(f"/documents/{up['documentId']}", headers=auth(key)).get_json()
    assert doc["document"]["filename"] == "a.pdf"
    assert doc["stages"] == []


def test_get_other_tenants_batch_is_404(client, make_tenant):
    _, key_a = make_tenant("tenant-a")
    _, key_b = make_tenant("tenant-b")
    batch_id = create_batch(client, key_a, [file_spec("a.pdf", pdf_bytes())]).get_json()["batchId"]
    assert_envelope(client.get(f"/batches/{batch_id}", headers=auth(key_b)), 404, "NOT_FOUND")


# --- CLI ---------------------------------------------------------------------------------------


def test_seed_tenant_cli_prints_a_working_key(app, client):
    result = app.test_cli_runner().invoke(args=["seed-tenant", "--name", "demo", "--max-file-mb", "3"])
    assert result.exit_code == 0, result.output
    key = next(line.split("=", 1)[1] for line in result.output.splitlines() if line.startswith("api_key="))
    assert client.get("/batches/1", headers=auth(key)).status_code == 404  # authenticated, batch absent

    again = app.test_cli_runner().invoke(args=["seed-tenant", "--name", "demo"])
    assert again.exit_code != 0
    assert "already exists" in again.output


# --- helpers -----------------------------------------------------------------------------------


def run_concurrently(app, key: str, document_ids: list[int]) -> list[int]:
    """Fire one complete per id from separate threads, released together by a barrier."""
    barrier = threading.Barrier(len(document_ids))
    statuses: list[int | None] = [None] * len(document_ids)

    def worker(i: int, doc_id: int) -> None:
        client = app.test_client()
        barrier.wait()
        statuses[i] = complete(client, key, doc_id).status_code

    threads = [threading.Thread(target=worker, args=(i, d)) for i, d in enumerate(document_ids)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return statuses


# --- GET /documents/:id/url (P4: citation chips) ------------------------------------------------


def test_document_url_is_tenant_scoped(client, make_tenant, fake_storage):
    _, key_a = make_tenant("tenant-a")
    _, key_b = make_tenant("tenant-b")
    up = create_batch(client, key_a, [file_spec("Policy v2.pdf", pdf_bytes())]).get_json()["uploads"][0]

    own = client.get(f"/documents/{up['documentId']}/url", headers=auth(key_a))
    assert own.status_code == 200
    body = own.get_json()
    assert body["url"].startswith("https://fake-s3.test/tenant/") and "response-content-type=application/pdf" in body["url"]
    assert body["expiresIn"] == 900

    assert_envelope(client.get(f"/documents/{up['documentId']}/url", headers=auth(key_b)), 404, "NOT_FOUND")
