"""JSON shapes for API responses (camelCase, as in docs/specs.md §6)."""

from ..models import Batch, DeadLetter, Document, DocumentStage


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
        "activeVersion": doc.active_version,
    }


def stage_view(stage: DocumentStage) -> dict:
    return {
        "stage": stage.stage,
        "version": stage.version,
        "status": stage.status,
        "attempts": stage.attempts,
        "lastError": stage.last_error[:300] if stage.last_error else None,
        "startedAt": stage.started_at.isoformat() if stage.started_at else None,
        "finishedAt": stage.finished_at.isoformat() if stage.finished_at else None,
    }


def dead_letter_view(dl: DeadLetter, filename: str) -> dict:
    return {
        "id": dl.id,
        "documentId": dl.document_id,
        "filename": filename,
        "stage": dl.stage,
        "version": dl.version,
        "kind": dl.kind,
        "errorCode": dl.error_code,
        "error": dl.error[:300],
        "createdAt": dl.created_at.isoformat() if dl.created_at else None,
        "replayedAt": dl.replayed_at.isoformat() if dl.replayed_at else None,
    }
