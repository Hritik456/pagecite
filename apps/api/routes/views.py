"""JSON shapes for API responses (camelCase, as in docs/specs.md §6)."""

from ..models import Batch, Document, DocumentStage


def batch_view(batch: Batch) -> dict:
    return {
        "id": batch.id,
        "status": batch.status,
        "total": batch.total,
        "succeeded": batch.succeeded,
        "failed": batch.failed,
        "createdAt": batch.created_at.isoformat() if batch.created_at else None,
    }


def document_view(doc: Document) -> dict:
    return {
        "id": doc.id,
        "batchId": doc.batch_id,
        "filename": doc.filename,
        "sizeBytes": doc.size_bytes,
        "mime": doc.mime,
        "status": doc.status,
        "processingVersion": doc.processing_version,
    }


def stage_view(stage: DocumentStage) -> dict:
    return {
        "stage": stage.stage,
        "version": stage.version,
        "status": stage.status,
        "attempts": stage.attempts,
        "startedAt": stage.started_at.isoformat() if stage.started_at else None,
        "finishedAt": stage.finished_at.isoformat() if stage.finished_at else None,
    }
