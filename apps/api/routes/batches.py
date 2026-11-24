"""POST /batches (quota checks + presigned PUT URLs) and GET /batches/:id."""

import logging
import uuid

from flask import Blueprint, jsonify, request
from sqlalchemy import select

from ..errors import AppError
from ..keys import object_key
from ..models import Batch, BatchStatus, Document, DocumentStage, DocumentStatus
from ..quotas import BatchRequest, check_batch, sha256_hex_to_b64
from ..state import current_tenant, get_session, get_storage
from ..storage import required_put_headers
from .views import batch_view, document_view, stage_view

bp = Blueprint("batches", __name__)
log = logging.getLogger(__name__)


@bp.post("/batches")
def create_batch():
    body = BatchRequest.model_validate(request.get_json())
    tenant = current_tenant()
    # Quotas run before any row or URL exists: an oversized file is rejected before the upload.
    check_batch(body.files, tenant.limits)

    session = get_session()
    with session.begin():
        batch = Batch(tenant_id=tenant.id, status=BatchStatus.UPLOADING, total=len(body.files))
        session.add(batch)
        session.flush()
        docs = []
        for spec in body.files:
            doc = Document(
                batch_id=batch.id,
                tenant_id=tenant.id,
                # object_key is NOT NULL UNIQUE but needs the document id: a placeholder, replaced
                # before commit, so it is never visible outside this transaction.
                object_key=f"pending/{uuid.uuid4().hex}",
                filename=spec.name,
                sha256=spec.sha256.lower(),
                size_bytes=spec.size,
                mime=spec.mime,
                status=DocumentStatus.CREATED,
            )
            session.add(doc)
            docs.append(doc)
        session.flush()
        for doc in docs:
            doc.object_key = object_key(tenant.id, batch.id, doc.id, doc.filename)

    # Presigning is a local signature computation (no network call), so it runs after the commit.
    storage = get_storage()
    uploads = []
    for doc in docs:
        checksum = sha256_hex_to_b64(doc.sha256)
        uploads.append(
            {
                "documentId": doc.id,
                "presignedUrl": storage.presign_put(doc.object_key, doc.mime, checksum),
                "headers": required_put_headers(doc.mime, checksum),
            }
        )
    log.info(
        "batch_created tenant_id=%s batch_id=%s files=%s bytes=%s",
        tenant.id, batch.id, len(docs), sum(d.size_bytes for d in docs),
    )
    return jsonify({"batchId": batch.id, "uploads": uploads}), 201


@bp.get("/batches/<int:batch_id>")
def get_batch(batch_id: int):
    tenant = current_tenant()
    session = get_session()
    batch = session.scalar(select(Batch).where(Batch.id == batch_id, Batch.tenant_id == tenant.id))
    if batch is None:
        raise AppError("NOT_FOUND", "batch not found", 404)
    docs = session.scalars(select(Document).where(Document.batch_id == batch.id).order_by(Document.id)).all()
    # Per-stage progress (spec §6) for every document's current version, in one query.
    stages = session.scalars(
        select(DocumentStage)
        .join(Document, Document.id == DocumentStage.document_id)
        .where(Document.batch_id == batch.id, DocumentStage.version == Document.processing_version)
        .order_by(DocumentStage.document_id, DocumentStage.id)
    ).all()
    by_doc: dict[int, list[dict]] = {}
    for stage in stages:
        by_doc.setdefault(stage.document_id, []).append(stage_view(stage))
    return jsonify(
        {
            "batch": batch_view(batch),
            "documents": [{**document_view(d), "stages": by_doc.get(d.id, [])} for d in docs],
        }
    )
