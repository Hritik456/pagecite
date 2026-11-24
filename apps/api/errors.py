"""Error envelope: { "error": { "code", "message", "request_id" } } for every failure."""

import logging

from flask import Flask, g, jsonify
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

log = logging.getLogger(__name__)


class AppError(Exception):
    """An expected failure with a stable machine-readable code. Has no Flask dependency."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def error_response(code: str, message: str, status: int):
    body = {"error": {"code": code, "message": message, "request_id": g.get("request_id")}}
    return jsonify(body), status


def _validation_message(exc: ValidationError) -> str:
    # Location + reason only: pydantic's default text echoes the submitted input.
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err["loc"]) or "body"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(AppError)
    def _app_error(exc: AppError):
        return error_response(exc.code, exc.message, exc.status)

    @app.errorhandler(ValidationError)
    def _validation_error(exc: ValidationError):
        return error_response("VALIDATION_ERROR", _validation_message(exc), 422)

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        code = (exc.name or "error").upper().replace(" ", "_")
        return error_response(code, exc.description or exc.name, exc.code or 500)

    @app.errorhandler(Exception)
    def _unexpected(exc: Exception):
        log.exception("unhandled_error request_id=%s type=%s", g.get("request_id"), type(exc).__name__)
        return error_response("INTERNAL_ERROR", "internal server error", 500)
