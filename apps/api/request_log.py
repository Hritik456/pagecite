"""Request ids and one log line per request: ids, sizes and timings only (never filenames or content)."""

import logging
import re
import time
import uuid

from flask import Flask, g, request

log = logging.getLogger("pagecite.request")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")


def register_request_hooks(app: Flask) -> None:
    @app.before_request
    def _start():
        incoming = request.headers.get("X-Request-ID", "")
        g.request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        g.started = time.perf_counter()

    @app.after_request
    def _finish(response):
        response.headers["X-Request-ID"] = g.get("request_id", "")
        if request.endpoint == "health":  # polled by the container healthcheck every few seconds
            return response
        tenant = g.get("tenant")
        rule = request.url_rule.rule if request.url_rule else "-"
        ms = (time.perf_counter() - g.get("started", time.perf_counter())) * 1000
        log.info(
            "request_id=%s tenant_id=%s method=%s route=%s status=%s ms=%.1f",
            g.get("request_id"),
            tenant.id if tenant else "-",
            request.method,
            rule,
            response.status_code,
            ms,
        )
        return response
