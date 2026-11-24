"""Shared fixtures.

- Pure-logic tests (tests/unit) use none of these and need no Docker.
- DB tests use the compose MySQL's `pagecite_test` database (schema from Flyway, service flyway-test),
  emptied before each test, with S3 replaced by FakeStorage.
- Integration tests (tests/integration, -m integration) also use the real MinIO, see that conftest.
"""

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from apps.api import create_app
from apps.api.cli import create_tenant
from apps.api.config import Settings
from apps.api.storage import Storage
from tests.fakes import FakeStorage

START_HINT = "run `docker compose up -d mysql flyway-test` first (see README, Testing)"
TABLES = [
    "outbox_events", "eval_runs", "dead_letters", "processed_jobs", "chunks",
    "document_stages", "documents", "batches", "tenants",
]


class TestEnv(BaseSettings):
    """Test-only settings, read from env vars / .env."""

    __test__ = False  # not a test class
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    test_database_url: str = "mysql+pymysql://pagecite:pagecite-dev@localhost:3306/pagecite_test"
    s3_test_bucket: str = "pagecite-test"


@pytest.fixture(scope="session")
def test_env() -> TestEnv:
    return TestEnv()


@pytest.fixture(scope="session")
def db_engine(test_env: TestEnv) -> Iterator[Engine]:
    engine = create_engine(test_env.test_database_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            applied = conn.execute(
                text("SELECT COUNT(*) FROM flyway_schema_history WHERE success = 1")
            ).scalar()
    except OperationalError as exc:
        pytest.fail(f"cannot use the test database ({exc.orig}); {START_HINT}", pytrace=False)
    if not applied:
        pytest.fail(f"the test database has no Flyway migrations applied; {START_HINT}", pytrace=False)
    yield engine
    engine.dispose()


@pytest.fixture
def clean_db(db_engine: Engine) -> Engine:
    # DELETE (children first) in one transaction: much faster than TRUNCATE, which recreates
    # each table. Ids keep counting up across tests; no test depends on their values.
    with db_engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"DELETE FROM {table}"))
    return db_engine


def make_test_app(test_env: TestEnv, storage: Storage, **overrides) -> Flask:
    settings = Settings(
        database_url=test_env.test_database_url,
        s3_bucket=overrides.pop("s3_bucket", "unused-in-db-tests"),
        log_level="WARNING",
        **overrides,
    )
    app = create_app(settings=settings, storage=storage)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def app(clean_db: Engine, test_env: TestEnv, fake_storage: FakeStorage) -> Iterator[Flask]:
    app = make_test_app(test_env, fake_storage)
    yield app
    app.extensions["pagecite"].engine.dispose()


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def make_tenant(clean_db: Engine):
    """make_tenant(name, max_file_mb=5, max_batch_files=10) -> (tenant_id, api_key)."""

    def _make(name: str = "acme", max_file_mb: int = 5, max_batch_files: int = 10) -> tuple[int, str]:
        with Session(clean_db) as session, session.begin():
            tenant, key = create_tenant(session, name, max_file_mb, max_batch_files)
            return tenant.id, key

    return _make


@pytest.fixture
def db_scalar(clean_db: Engine):
    """db_scalar(sql, **params): run one SELECT against the test DB and return the first column."""

    def _q(sql: str, **params):
        with clean_db.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    return _q
