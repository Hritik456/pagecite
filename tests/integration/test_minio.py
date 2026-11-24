"""End-to-end upload flow against the real MinIO: presign -> PUT -> complete."""

import pytest
import requests

from tests.helpers import auth, file_spec, pdf_bytes

pytestmark = pytest.mark.integration


def create_batch(client, key, files):
    resp = client.post("/batches", json={"files": files}, headers=auth(key))
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def put(upload: dict, data: bytes, headers: dict | None = None) -> requests.Response:
    return requests.put(upload["presignedUrl"], data=data, headers=upload["headers"] if headers is None else headers, timeout=30)


def s3_error_code(resp: requests.Response) -> str:
    return resp.text.split("<Code>")[1].split("</Code>")[0] if "<Code>" in resp.text else ""


def test_five_pdfs_end_up_uploaded_with_five_outbox_rows(s3_app, make_tenant, db_scalar):
    client = s3_app.test_client()
    _, key = make_tenant(max_file_mb=5, max_batch_files=20)
    blobs = [pdf_bytes(f"doc-{i}", size=2048 + i) for i in range(5)]
    body = create_batch(client, key, [file_spec(f"doc {i}.pdf", d) for i, d in enumerate(blobs)])

    for upload, data in zip(body["uploads"], blobs):
        assert put(upload, data).status_code == 200
    for upload in body["uploads"]:
        resp = client.post(f"/documents/{upload['documentId']}/complete", headers=auth(key))
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["document"]["status"] == "UPLOADED"

    assert db_scalar("SELECT COUNT(*) FROM documents WHERE status = 'UPLOADED'") == 5
    assert db_scalar("SELECT COUNT(*) FROM outbox_events WHERE type = 'document.uploaded'") == 5
    assert db_scalar("SELECT status FROM batches WHERE id = :id", id=body["batchId"]) == "PROCESSING"

    # A duplicate complete changes nothing.
    again = client.post(f"/documents/{body['uploads'][0]['documentId']}/complete", headers=auth(key))
    assert again.status_code == 200
    assert db_scalar("SELECT COUNT(*) FROM outbox_events") == 5


def test_minio_rejects_bytes_that_do_not_match_the_declared_sha256(s3_app, make_tenant, db_scalar):
    client = s3_app.test_client()
    _, key = make_tenant()
    real = pdf_bytes("real")
    upload = create_batch(client, key, [file_spec("a.pdf", real)])["uploads"][0]

    resp = put(upload, pdf_bytes("tampered", size=len(real)))
    assert resp.status_code == 400
    assert s3_error_code(resp) == "XAmzContentChecksumMismatch"

    complete = client.post(f"/documents/{upload['documentId']}/complete", headers=auth(key))
    assert complete.status_code == 409
    assert complete.get_json()["error"]["code"] == "NOT_UPLOADED"
    assert db_scalar("SELECT COUNT(*) FROM outbox_events") == 0


def test_minio_rejects_a_larger_body_than_declared(s3_app, make_tenant):
    client = s3_app.test_client()
    _, key = make_tenant(max_file_mb=1)
    small = pdf_bytes("small")
    upload = create_batch(client, key, [file_spec("a.pdf", small)])["uploads"][0]

    # The signed checksum pins the exact bytes, so the declared size cannot be exceeded either.
    resp = put(upload, small + b"0" * (2 * 1024 * 1024))
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "drop", ["x-amz-checksum-sha256", "Content-Type"], ids=["without-checksum", "without-content-type"]
)
def test_presigned_put_requires_the_signed_headers(s3_app, make_tenant, drop):
    client = s3_app.test_client()
    _, key = make_tenant()
    data = pdf_bytes()
    upload = create_batch(client, key, [file_spec("a.pdf", data)])["uploads"][0]

    headers = {k: v for k, v in upload["headers"].items() if k != drop}
    if drop == "Content-Type":
        headers["Content-Type"] = "application/octet-stream"
    resp = put(upload, data, headers)
    assert resp.status_code in (400, 403), resp.text
