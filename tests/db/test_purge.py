"""purge-tenant (P5 load-test cleanup): local/test only, tenant-scoped, refused while work is in flight."""

import fnmatch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from apps.api.purge import PurgeRefused, purge_tenant
from apps.api.search_index import IndexConfig
from tests.conftest import make_test_app

INDEX = IndexConfig(name="idx:unused", prefix="chunk:", dim=4)


class KeyStore:
    """Just enough Redis for delete_tenant_keys: scan_iter(match) + unlink."""

    def __init__(self, keys):
        self.keys = set(keys)

    def scan_iter(self, match, count=None):
        return [k for k in sorted(self.keys) if fnmatch.fnmatchcase(k, match)]

    def unlink(self, *keys):
        n = len(self.keys & set(keys))
        self.keys -= set(keys)
        return n


def seed(engine, tenant_id: int, statuses: list[str]) -> list[int]:
    """One batch with a document per status, each with a stage, chunk, page, DLQ row, job key and outbox row."""
    with engine.begin() as conn:
        batch_id = conn.execute(text("INSERT INTO batches (tenant_id, total) VALUES (:t, :n)"),
                                {"t": tenant_id, "n": len(statuses)}).lastrowid
        ids = []
        for i, status in enumerate(statuses):
            doc_id = conn.execute(text(
                "INSERT INTO documents (batch_id, tenant_id, object_key, filename, sha256, size_bytes, mime, status) "
                "VALUES (:b, :t, :k, 'f.pdf', REPEAT('a', 64), 1, 'application/pdf', :s)"),
                {"b": batch_id, "t": tenant_id, "k": f"tenants/{tenant_id}/b{batch_id}/d{i}.pdf", "s": status}).lastrowid
            ids.append(doc_id)
            conn.execute(text("INSERT INTO document_stages (document_id, stage, version) VALUES (:d, 'parse', 1)"), {"d": doc_id})
            conn.execute(text("INSERT INTO chunks (document_id, tenant_id, version, ord, page_from, page_to, text, token_count) "
                              "VALUES (:d, :t, 1, 0, 1, 1, 'x', 1)"), {"d": doc_id, "t": tenant_id})
            conn.execute(text("INSERT INTO document_pages (document_id, version, page_no, text) VALUES (:d, 1, 1, 'x')"),
                         {"d": doc_id})
            conn.execute(text("INSERT INTO dead_letters (document_id, stage, version, error) VALUES (:d, 'embed', 1, 'e')"),
                         {"d": doc_id})
            conn.execute(text("INSERT INTO processed_jobs (job_key) VALUES (:k)"), {"k": f"doc:{doc_id}:parse:v1"})
            conn.execute(text("INSERT INTO outbox_events (type, aggregate_id, payload) VALUES ('document.uploaded', :d, '{}')"),
                         {"d": doc_id})
    return ids


def count(engine, sql: str, **params) -> int:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def test_purge_removes_only_the_target_tenant(clean_db, make_tenant, fake_storage):
    a, _ = make_tenant("a")
    b, _ = make_tenant("b")
    a_docs = seed(clean_db, a, ["INDEXED", "FAILED", "CANCELLED", "CREATED"])
    b_docs = seed(clean_db, b, ["INDEXED"])
    # b's ids may be a's ids with a suffix (doc 1 vs doc 12): the job-key and index patterns must not overlap
    keys = KeyStore([f"chunk:{a}:1", f"chunk:{a}:2", f"chunk:{b}:3", f"chunk:{a}{b}:4"])
    for d in a_docs + b_docs:
        fake_storage.objects[f"k{d}"] = b"x"

    report = purge_tenant(sessionmaker(clean_db), keys, INDEX, fake_storage, a)

    assert (report.documents, report.batches, report.redis_keys, report.s3_objects) == (4, 1, 2, 4)
    assert report.processed_jobs == 4 and report.outbox_events == 4
    assert keys.keys == {f"chunk:{b}:3", f"chunk:{a}{b}:4"}
    assert all(k.startswith(f"tenants/{a}/") for k in fake_storage.deleted)
    for table in ("documents", "batches", "chunks"):
        assert count(clean_db, f"SELECT COUNT(*) FROM {table} WHERE tenant_id = :t", t=a) == 0
    for table in ("document_stages", "document_pages", "dead_letters"):  # gone with their documents
        assert count(clean_db, f"SELECT COUNT(*) FROM {table} WHERE document_id IN :ids".replace(
            ":ids", "(" + ",".join(map(str, a_docs)) + ")")) == 0
    # tenant b untouched
    assert count(clean_db, "SELECT COUNT(*) FROM chunks WHERE tenant_id = :t", t=b) == 1
    assert count(clean_db, "SELECT COUNT(*) FROM processed_jobs WHERE job_key = :k", k=f"doc:{b_docs[0]}:parse:v1") == 1
    assert count(clean_db, "SELECT COUNT(*) FROM outbox_events WHERE aggregate_id = :d", d=b_docs[0]) == 1
    assert count(clean_db, "SELECT COUNT(*) FROM tenants WHERE id = :t", t=a) == 1  # the tenant itself is kept


def test_purge_is_refused_while_a_document_is_in_flight(clean_db, make_tenant, fake_storage):
    a, _ = make_tenant("a")
    seed(clean_db, a, ["INDEXED", "EMBEDDING"])
    keys = KeyStore([f"chunk:{a}:1"])
    with pytest.raises(PurgeRefused, match="in flight"):
        purge_tenant(sessionmaker(clean_db), keys, INDEX, fake_storage, a)
    assert keys.keys == {f"chunk:{a}:1"} and fake_storage.deleted == []  # nothing touched
    assert count(clean_db, "SELECT COUNT(*) FROM documents WHERE tenant_id = :t", t=a) == 2


def test_rerun_after_a_purge_is_a_no_op(clean_db, make_tenant, fake_storage):
    a, _ = make_tenant("a")
    seed(clean_db, a, ["INDEXED"])
    purge_tenant(sessionmaker(clean_db), KeyStore([]), INDEX, fake_storage, a)
    again = purge_tenant(sessionmaker(clean_db), KeyStore([]), INDEX, fake_storage, a)
    assert (again.documents, again.batches, again.s3_objects) == (0, 0, 0)


def test_cli_refuses_production_before_touching_anything(clean_db, make_tenant, test_env, fake_storage):
    a, _ = make_tenant("a")
    seed(clean_db, a, ["INDEXED"])
    app = make_test_app(test_env, fake_storage, app_env="production")
    result = app.test_cli_runner().invoke(args=["purge-tenant", "--tenant", str(a), "--yes"])
    assert result.exit_code != 0 and "APP_ENV=production" in result.output
    assert count(clean_db, "SELECT COUNT(*) FROM documents WHERE tenant_id = :t", t=a) == 1
    app.extensions["pagecite"].engine.dispose()


def test_cli_needs_yes(app, clean_db, make_tenant):
    a, _ = make_tenant("a")
    seed(clean_db, a, ["INDEXED"])
    result = app.test_cli_runner().invoke(args=["purge-tenant", "--tenant", str(a)])
    assert result.exit_code != 0 and "--yes" in result.output
    assert count(clean_db, "SELECT COUNT(*) FROM documents WHERE tenant_id = :t", t=a) == 1
