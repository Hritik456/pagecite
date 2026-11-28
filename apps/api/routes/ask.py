"""POST /ask: a cited answer over the tenant's documents, streamed as Server-Sent Events.

    request:  {"question": "...", "stream": true}
    stream:   event: token      data: {"text": "..."}          (repeated)
              event: citations  data: {"citations": [{n, document_id, title, page, page_to}], "abstained": bool}
              event: error      data: {"code", "message", "request_id"}   (only if the model call fails)
    stream=false returns the same content as JSON: {answer, citations, abstained}.

Retrieval (decompose, embed, FT.HYBRID, MySQL rows, rerank) finishes before the response starts, and
the request's DB session is released then: the stream holds no pooled MySQL connection.

Public demo limits (P5B, apps/api/limits.py), when configured, in this order:
1. per-IP rate limit, counted for every attempt, before the body is even validated (429 RATE_LIMITED)
2. question length <= ASK_MAX_CHARS (422 QUESTION_TOO_LONG)
3. reserve the worst-case cost against the daily spend cap (429 DEMO_LIMIT_REACHED)
4. retrieve + answer; the reservation is settled to the actual cost when the request ends
   (in the stream's `finally`), using only Redis: still no DB connection during the stream.
"""

import json
import logging
import time
from collections.abc import Iterator

from flask import Blueprint, Response, g, jsonify, request, stream_with_context
from pydantic import BaseModel, Field

from ..errors import AppError
from ..retrieval.pipeline import RetrievalConfig, UpstreamError, answer_events, retrieve
from ..retrieval.pricing import UnknownModelPrice, cost_of_calls
from ..retrieval.search import IndexUnavailable
from ..retrieval.usage import UsageLog
from ..state import app_state, close_session, current_tenant, get_limits, get_retrieval

bp = Blueprint("ask", __name__)
log = logging.getLogger("pagecite.limits")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)  # the real cap is ASK_MAX_CHARS, below
    stream: bool = True


def sse(event: str, data: dict) -> str:
    """One SSE frame. JSON keeps newlines inside a single `data:` line."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _cost_known(usage: UsageLog, blocks) -> bool:
    """Every model call of this request has reported its usage (the answer call last; with no blocks the
    API abstains without one). Otherwise the client left mid-answer and the reservation stands."""
    return not blocks or any(c.call == "answer" for c in usage.calls)


@bp.post("/ask")
def ask():
    limits = get_limits()
    if limits is not None:
        limits.check_rate(request.remote_addr)  # every attempt counts, valid or not
    body = AskRequest.model_validate(request.get_json())
    question = " ".join(body.question.split())
    if not question:
        raise AppError("VALIDATION_ERROR", "question: must not be blank", 422)
    max_chars = app_state().settings.ask_max_chars
    if len(question) > max_chars:
        raise AppError("QUESTION_TOO_LONG", f"questions are limited to {max_chars} characters", 422)

    tenant = current_tenant()
    deps = get_retrieval()
    request_id = g.request_id
    usage = UsageLog(request_id, tenant.id)
    started = time.perf_counter()
    reservation = limits.reserve() if limits is not None else None

    def settle(blocks) -> None:
        if limits is None:
            return
        try:
            cost, known = cost_of_calls(usage.calls), _cost_known(usage, blocks)
        except UnknownModelPrice:  # can't price a call: count it as unknown, so the reservation stands
            log.warning("spend_unpriced_call request_id=%s", request_id)
            cost, known = 0.0, False
        limits.settle(reservation, cost, known)

    try:
        retrieved = retrieve(deps, tenant.id, question, RetrievalConfig.from_settings(deps.settings), usage)
    except IndexUnavailable as exc:
        settle([])
        raise AppError("INDEX_UNAVAILABLE", "the search index is unavailable; try again later", 503) from exc
    except UpstreamError as exc:
        settle([])
        raise AppError("UPSTREAM_ERROR", "the embedding service failed; try again later", 502) from exc
    except BaseException:
        settle(["unknown"])  # unexpected: keep the reservation
        raise

    # From here on only plain data is used; give back any pooled connection before streaming.
    close_session()
    events = answer_events(deps, question, retrieved.blocks, usage)

    if not body.stream:
        try:
            return _as_json(events, usage, started, request_id)
        finally:
            settle(retrieved.blocks)

    def generate() -> Iterator[str]:
        try:
            for name, data in events:
                if name == "error":
                    data = {**data, "request_id": request_id}
                yield sse(name, data)
        finally:
            usage.summary((time.perf_counter() - started) * 1000)
            settle(retrieved.blocks)

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
