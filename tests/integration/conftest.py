"""Integration fixtures: the real MinIO (test bucket), the MySQL test database, the real Redis
(test index) and the P2 pipeline run in-process (eager Celery)."""

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
import redis
import requests
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask
from sqlalchemy import Engine

from apps.api.config import Settings
from apps.api.search_index import IndexConfig, delete_all_keys, drop_index, make_redis
from apps.api.storage import S3Storage
from apps.worker.celery_app import app as celery
from apps.worker.context import celery_enqueue, tesseract_ocr, use_context
from apps.worker.relay import relay_once
from tests.conftest import TEST_INDEX_NAME, TEST_INDEX_PREFIX, TestEnv, make_test_app, make_worker_ctx
from tests.helpers import auth, file_spec

START_HINT = "run `docker compose up -d mysql flyway-test minio minio-init` first (see README, Testing)"


@pytest.fixture(scope="session")
def s3_settings(test_env: TestEnv) -> Settings:
    # S3 values (endpoint, credentials) come from .env; the test DB and bucket from TestEnv.
    return Settings(database_url=test_env.test_database_url, s3_bucket=test_env.s3_test_bucket)


@pytest.fixture(scope="session")
def s3_storage(s3_settings: Settings) -> S3Storage:
    storage = S3Storage(s3_settings)
    try:
        storage._client.head_bucket(Bucket=storage.bucket)
    except (ClientError, BotoCoreError) as exc:
        pytest.fail(f"cannot reach MinIO bucket {storage.bucket!r} ({exc}); {START_HINT}", pytrace=False)
    return storage


@pytest.fixture
def empty_bucket(s3_storage: S3Storage, s3_settings: Settings) -> None:
    """Remove every object in the test bucket so a test never sees another run's uploads."""
    bucket = boto3.resource(
        "s3",
        endpoint_url=s3_settings.s3_endpoint_url,
        region_name=s3_settings.aws_region,
        aws_access_key_id=s3_settings.aws_access_key_id,
        aws_secret_access_key=s3_settings.aws_secret_access_key,
    ).Bucket(s3_storage.bucket)
    bucket.objects.all().delete()


@pytest.fixture
def s3_app(clean_db: Engine, empty_bucket: None, test_env: TestEnv, s3_storage: S3Storage) -> Iterator[Flask]:
    app = make_test_app(test_env, s3_storage, s3_bucket=s3_storage.bucket)
    yield app
    app.extensions["pagecite"].engine.dispose()


@pytest.fixture
def redis_index(s3_settings: Settings) -> Iterator[tuple[redis.Redis, IndexConfig]]:
    """The real Redis, under a test-only index name and key prefix; emptied before and after."""
    r = make_redis(s3_settings.redis_url)
    try:
        r.ping()
    except redis.ConnectionError as exc:
        pytest.fail(f"cannot reach Redis ({exc}); run `docker compose up -d redis`", pytrace=False)
    cfg = IndexConfig(name=TEST_INDEX_NAME, prefix=TEST_INDEX_PREFIX, dim=s3_settings.embed_dim)

    def reset() -> None:
        drop_index(r, cfg)
        delete_all_keys(r, cfg)

    reset()
    yield r, cfg
    reset()


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


def upload_samples(client, key: str, only: list[str] | None = None) -> dict[str, int]:
    names = only or [m["file"] for m in MANIFEST]
    files = [(name, (SAMPLES / name).read_bytes()) for name in names]
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
    """All samples uploaded and pushed through the relay; eager Celery runs every stage inline."""
    tenant_id, key = make_tenant(max_file_mb=5, max_batch_files=20)
    ids = upload_samples(s3_app.test_client(), key)
    with worker_ctx.sessions() as session:
        assert relay_once(session, worker_ctx.enqueue) == len(MANIFEST)
    return tenant_id, ids
