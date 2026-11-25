# Build context: repo root (see docker-compose.yml).
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first (cached unless the lockfile changes), then the code.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY apps ./apps
RUN uv sync --frozen --no-dev --no-editable

RUN useradd --system --no-create-home app
USER app

EXPOSE 8000
# gthread: a streaming /ask holds one thread, not a whole worker process (2 workers x 8 threads).
# --timeout is the worker heartbeat; each upstream call has its own timeout (answer 60 s, JSON 20 s).
# --keep-alive 75 must stay above Caddy's upstream keepalive (30 s, Caddyfile): the proxy closes idle
# connections first, so it never sends a request on one gunicorn is closing (reset -> 502).
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--worker-class", "gthread", "--threads", "8", "--timeout", "120", "--keep-alive", "75", "apps.api:create_app()"]
