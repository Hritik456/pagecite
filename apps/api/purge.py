"""Purge one tenant's documents from every store: local/test cleanup only (the P5 load test).

Not an API endpoint, by design: it is a CLI command (`flask purge-tenant --tenant <id> --yes`) that
refuses APP_ENV=production before it opens any connection.

Safety:
- Refused while any of the tenant's documents is in flight (UPLOADED .. RETRY_WAIT, CANCEL_REQUESTED):
  a worker could otherwise write chunks or index keys after the purge. CREATED (never uploaded),
  INDEXED, FAILED and CANCELLED documents have no worker activity.
- Only this tenant: Redis keys by `{prefix}{tenant}:*` (the colon stops `chunk:1:*` matching tenant
  12), S3 objects by the documents' own keys, MySQL rows by tenant id / document id.
- Order: Redis (derived), then S3, then MySQL (the truth) in one transaction. If a step fails, the
  MySQL rows are still there, so rerunning finds everything again: every step is idempotent.
- The tenant row and its key are kept.
"""

from dataclasses import dataclass

import redis
from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Batch, Document, DocumentStatus
from .search_index import IndexConfig, delete_tenant_keys
from .storage import Storage

IDLE_STATUSES = frozenset({
    DocumentStatus.CREATED, DocumentStatus.INDEXED, DocumentStatus.FAILED, DocumentStatus.CANCELLED,
})


class PurgeRefused(Exception):
    pass


@dataclass(frozen=True)
class PurgeReport:
    tenant_id: int
    documents: int
    batches: int
    redis_keys: int
    s3_objects: int
    processed_jobs: int
    outbox_events: int


def refuse_in_production(app_env: str) -> None:
    if app_env.lower() == "production":
        raise PurgeRefused("purge-tenant is a local/test tool and is refused with APP_ENV=production")


def purge_tenant(
    sessions: sessionmaker[Session], r: redis.Redis, index: IndexConfig, storage: Storage, tenant_id: int,
) -> PurgeReport:
    with sessions() as session:  # short read: what exists and whether anything is in flight
        docs = session.execute(
            select(Document.id, Document.object_key, Document.status).where(Document.tenant_id == tenant_id)
        ).all()
    busy = [d for d in docs if d.status not in IDLE_STATUSES]
    if busy:
        raise PurgeRefused(f"{len(busy)} document(s) still in flight (e.g. document {busy[0].id} {busy[0].status}); "
                           "wait for them to finish or cancel the batch first")

    redis_keys = delete_tenant_keys(r, index, tenant_id)
    for d in docs:
        storage.delete(d.object_key)  # a missing object is not an error

    doc_ids = [d.id for d in docs]
    with sessions() as session, session.begin():
        jobs = outbox = 0
        for i in range(0, len(doc_ids), 500):
            part = doc_ids[i:i + 500]
            for doc_id in part:  # job keys are doc:{id}:{stage}:v{n}; the trailing colon pins the id
                jobs += session.execute(text("DELETE FROM processed_jobs WHERE job_key LIKE :p"),
                                        {"p": f"doc:{doc_id}:%"}).rowcount
            outbox += session.execute(
                text("DELETE FROM outbox_events WHERE type = 'document.uploaded' AND aggregate_id IN :ids")
                .bindparams(bindparam("ids", expanding=True)), {"ids": part}
            ).rowcount
        # chunks, document_pages, document_stages and dead_letters go with their document (ON DELETE CASCADE)
        n_docs = session.execute(delete(Document).where(Document.tenant_id == tenant_id)).rowcount
        n_batches = session.execute(delete(Batch).where(Batch.tenant_id == tenant_id)).rowcount
    return PurgeReport(tenant_id, n_docs, n_batches, redis_keys, len(docs), jobs, outbox)

