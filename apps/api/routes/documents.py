"""GET /documents/:id and POST /documents/:id/complete."""

import logging

from flask import Blueprint, jsonify
from sqlalchemy import func, select, update

from ..errors import AppError
from ..keys import tenant_prefix
from ..models import Batch, BatchStatus, Document, DocumentStage, DocumentStatus, OutboxEvent
from ..quotas import sha256_hex_to_b64
from ..state import current_tenant, get_session, get_storage
from .views import document_view, stage_view

bp = Blueprint("documents", __name__)
log = logging.getLogger(__name__)

OUTBOX_DOCUMENT_UPLOADED = "document.uploaded"


def _load_document(document_id: int) -> Document:
    tenant = current_tenant()
    doc = get_session().scalar(
        select(Document)
        .where(Document.id == document_id, Document.tenant_id == tenant.id)
        # Always read the row as stored: Core-style UPDATEs elsewhere bypass the identity map.
        .execution_options(populate_existing=True)
    )
    if doc is None:
        # Another tenant's document looks exactly like a missing one.
        raise AppError("NOT_FOUND", "document not found", 404)
    return doc


@bp.get("/documents/<int:document_id>")
def get_document(document_id: int):
    doc = _load_document(document_id)
    stages = get_session().scalars(
        select(DocumentStage)
        .where(DocumentStage.document_id == doc.id)
        .order_by(DocumentStage.version, DocumentStage.id)
    ).all()
    return jsonify({"document": document_view(doc), "stages": [stage_view(s) for s in stages]})


@bp.post("/documents/<int:document_id>/complete")
def complete_upload(document_id: int):
    tenant = current_tenant()
    session = get_session()
    storage = get_storage()

    doc = _load_document(document_id)
    if not doc.object_key.startswith(tenant_prefix(tenant.id)):
        raise AppError("OBJECT_KEY_MISMATCH", "document object key is outside the tenant prefix", 409)
    if doc.status != DocumentStatus.CREATED:
        # Duplicate (or late) complete: a no-op that reports the current state.
        return jsonify({"document": document_view(doc)}), 200

    key, size, sha256_hex, batch_id = doc.object_key, doc.size_bytes, doc.sha256, doc.batch_id
    version = doc.processing_version
    # End the read transaction before the network call to S3.
    session.rollback()

    info = storage.head(key)
    if info is None:
        raise AppError("NOT_UPLOADED", "the object has not been uploaded yet", 409)
    if info.size != size:
        storage.delete(key)
        raise AppError("SIZE_MISMATCH", f"uploaded size {info.size} != declared size {size}", 422)
    if info.checksum_sha256_b64 != sha256_hex_to_b64(sha256_hex):
        storage.delete(key)
        raise AppError("CHECKSUM_MISMATCH", "uploaded object's SHA-256 does not match the declared hash", 422)

    with session.begin():
        # Lock the batch row first: completes in one batch are serialised in a fixed lock order,
        # so the "no CREATED documents left" check below is exact and cannot deadlock.
        session.execute(select(Batch.id).where(Batch.id == batch_id).with_for_update())
        result = session.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.tenant_id == tenant.id,
                Document.status == DocumentStatus.CREATED,
            )
            .values(status=DocumentStatus.UPLOADED)
            .execution_options(synchronize_session=False)
        )
        transitioned = result.rowcount == 1
        if transitioned:
            # Same transaction as the status change: the event exists if and only if the transition did.
            session.add(
                OutboxEvent(
                    type=OUTBOX_DOCUMENT_UPLOADED,
                    aggregate_id=document_id,
                    payload={
                        "document_id": document_id,
                        "tenant_id": tenant.id,
                        "batch_id": batch_id,
                        "version": version,
                    },
                )
            )
            remaining = session.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.batch_id == batch_id, Document.status == DocumentStatus.CREATED)
                .with_for_update(read=True)
            )
            if remaining == 0:
                session.execute(
                    update(Batch)
                    .where(Batch.id == batch_id, Batch.status == BatchStatus.UPLOADING)
                    .values(status=BatchStatus.PROCESSING)
                    .execution_options(synchronize_session=False)
                )

    if transitioned:
        log.info("document_uploaded tenant_id=%s document_id=%s bytes=%s", tenant.id, document_id, size)
    doc = _load_document(document_id)
    return jsonify({"document": document_view(doc)}), 200
