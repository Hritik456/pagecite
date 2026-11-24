"""Integration fixtures: the real MinIO (test bucket) + the MySQL test database."""

from collections.abc import Iterator

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask
from sqlalchemy import Engine

from apps.api.config import Settings
from apps.api.storage import S3Storage
from tests.conftest import TestEnv, make_test_app

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
