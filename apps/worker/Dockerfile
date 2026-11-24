# Build context: repo root (see docker-compose.yml). One image for the relay and all stage workers.
FROM python:3.11-slim

# Tesseract for scanned pages (OCR runs only on pages without a text layer).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY apps ./apps
RUN uv sync --frozen --no-dev --no-editable

# Bake the tokenizer file into the image: workers never download it at runtime.
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" && chmod -R a+rX /opt/tiktoken

RUN useradd --system --no-create-home app
USER app

CMD ["celery", "-A", "apps.worker.celery_app", "worker", "--loglevel", "INFO"]
