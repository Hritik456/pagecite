"""Caddy must drop idle upstream connections before gunicorn does.

Otherwise Caddy can send a request on a pooled connection that gunicorn is closing: the API resets
it, Caddy answers 502, and a POST (e.g. /documents/:id/complete) is not retried. The P5 load test hit
exactly this with gunicorn's default 2 s keep-alive against Caddy's default 2 min.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _seconds(value: str) -> float:
    m = re.fullmatch(r"(\d+)(ms|s|m)?", value)
    assert m, value
    n, unit = int(m.group(1)), m.group(2) or "s"
    return n / 1000 if unit == "ms" else n * 60 if unit == "m" else n


def caddy_upstream_keepalive() -> float:
    m = re.search(r"reverse_proxy api:8000 \{.*?transport http \{.*?keepalive (\S+)", (ROOT / "Caddyfile").read_text(), re.S)
    assert m, "Caddyfile: reverse_proxy api:8000 needs `transport http { keepalive <duration> }`"
    return _seconds(m.group(1))


def gunicorn_keepalive() -> float:
    cmd = next(line for line in (ROOT / "apps/api/Dockerfile").read_text().splitlines() if line.startswith("CMD"))
    args = json.loads(cmd[len("CMD"):].strip())
    assert "--keep-alive" in args, "apps/api/Dockerfile: gunicorn needs an explicit --keep-alive"
    return float(args[args.index("--keep-alive") + 1])


def test_proxy_closes_idle_upstream_connections_before_gunicorn():
    assert caddy_upstream_keepalive() < gunicorn_keepalive()
