"""POST /ask: a cited answer over the tenant's documents, streamed as Server-Sent Events.

    request:  {"question": "...", "stream": true}
    stream:   event: token      data: {"text": "..."}          (repeated)
              event: citations  data: {"citations": [{n, document_id, title, page, page_to}], "abstained": bool}
              event: error      data: {"code", "message", "request_id"}   (only if the model call fails)
    stream=false returns the same content as JSON: {answer, citations, abstained}.

Retrieval (decompose, embed, FT.HYBRID, MySQL rows, rerank) finishes before the response starts, and
the request's DB session is released then: the stream holds no pooled MySQL connection.
"""

import json
import time
from collections.abc import Iterator

from flask import Blueprint, Response, g, jsonify, request, stream_with_context
from pydantic import BaseModel, Field

from ..errors import AppError
from ..retrieval.pipeline import RetrievalConfig, UpstreamError, answer_events, retrieve
from ..retrieval.search import IndexUnavailable
from ..retrieval.usage import UsageLog
from ..state import close_session, current_tenant, get_retrieval

bp = Blueprint("ask", __name__)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    stream: bool = True


def sse(event: str, data: dict) -> str:
    """One SSE frame. JSON keeps newlines inside a single `data:` line."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@bp.post("/ask")
def ask():
    body = AskRequest.model_validate(request.get_json())
    question = " ".join(body.question.split())
    if not question:
        raise AppError("VALIDATION_ERROR", "question: must not be blank", 422)

    tenant = current_tenant()
    deps = get_retrieval()
    request_id = g.request_id
    usage = UsageLog(request_id, tenant.id)
    started = time.perf_counter()

    try:
        retrieved = retrieve(deps, tenant.id, question, RetrievalConfig.from_settings(deps.settings), usage)
    except IndexUnavailable as exc:
        raise AppError("INDEX_UNAVAILABLE", "the search index is unavailable; try again later", 503) from exc
    except UpstreamError as exc:
        raise AppError("UPSTREAM_ERROR", "the embedding service failed; try again later", 502) from exc

    # From here on only plain data is used; give back any pooled connection before streaming.
    close_session()
    events = answer_events(deps, question, retrieved.blocks, usage)

    if not body.stream:
        return _as_json(events, usage, started, request_id)

    def generate() -> Iterator[str]:
        try:
            for name, data in events:
                if name == "error":
                    data = {**data, "request_id": request_id}
                yield sse(name, data)
        finally:
            usage.summary((time.perf_counter() - started) * 1000)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _as_json(events, usage: UsageLog, started: float, request_id: str):
    parts: list[str] = []
    result: dict = {"citations": [], "abstained": False}
    try:
        for name, data in events:
            if name == "token":
                parts.append(data["text"])
            elif name == "citations":
                result = data
            elif name == "error":
                status = 504 if data["code"] == "UPSTREAM_TIMEOUT" else 502
                raise AppError(data["code"], data["message"], status)
    finally:
        usage.summary((time.perf_counter() - started) * 1000)
    return jsonify({"answer": "".join(parts), **result})
