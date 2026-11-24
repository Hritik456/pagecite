"""GET /dead-letters: the tenant's final failures (P3), newest first. Replay is POST /documents/:id/replay."""

from flask import Blueprint, jsonify, request
from sqlalchemy import select

from ..models import DeadLetter, Document
from ..state import current_tenant, get_session
from .views import dead_letter_view

bp = Blueprint("dead_letters", __name__)


@bp.get("/dead-letters")
def list_dead_letters():
    tenant = current_tenant()
    only_open = request.args.get("all", "").lower() not in ("1", "true", "yes")
    q = (
        select(DeadLetter, Document.filename)
        .join(Document, Document.id == DeadLetter.document_id)
        .where(Document.tenant_id == tenant.id)
        .order_by(DeadLetter.id.desc())
        .limit(200)
    )
    if only_open:
        q = q.where(DeadLetter.replayed_at.is_(None))
    rows = get_session().execute(q).all()
    return jsonify({"deadLetters": [dead_letter_view(dl, filename) for dl, filename in rows]})
