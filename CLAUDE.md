# PageCite

An async RAG platform. Documents are uploaded straight to S3, then processed by staged Celery workers (parse → chunk → embed → index). Answers use hybrid retrieval, cite their sources, and are checked by an eval harness.
The full design is in `docs/specs.md`. Read only the sections the current phase prompt asks for.

## Stack (do not change without an ADR in docs/adr/)
- Python 3.11, Flask + SQLAlchemy 2.0, Celery 5 (Redis broker)
- MySQL 8.4 + Flyway (`db/migrations/V<n>__desc.sql`)
- Redis 8.4 Open Source Query Engine, for the vector + text index and `FT.HYBRID`
- S3 via boto3 (MinIO locally)
- OpenAI for embeddings, rerank and answers
- PDF/DOCX parsing with `pypdf`/`pdfplumber` + `python-docx`; Tesseract OCR only for scanned pages
- React + TypeScript + Vite + Tailwind for `apps/web`
- pytest
- Docker Compose with Caddy

## Layout
- `apps/api`: the Flask app
- `apps/worker`: Celery tasks, one module per stage
- `apps/web`
- `db/migrations`, `eval/` (golden set + runner), `load/`, `docs/`

## Commands
- `docker compose up -d` · `flask --app apps.api seed-tenant` · `flask --app apps.api reindex --tenant <id>`
- `pytest -q` · `pytest -m integration` · `python -m eval.run --config configs/hybrid_rerank.yaml`

## Invariants (never break)
- The API never parses, embeds or calls an LLM for ingestion inside a request. All of that goes through Celery.
- Each stage task is idempotent:
  - insert `processed_jobs(job_key)` first and exit if it is a duplicate
  - write output with upserts on UNIQUE keys, in the same transaction
- `acks_late=True` and `task_reject_on_worker_lost=True` on every task.
- MySQL holds the truth (chunk text). The Redis index is derived, and `reindex` can always rebuild it.
- Every retrieval query filters by `@tenant` inside the index query. Never filter after retrieval.
- Replays create a new `processing_version`. History is never overwritten.
- Answers cite context blocks as `[n]` and must say "not found in the documents" when the context doesn't support an answer.
- Never log document text or prompts. Log only ids, sizes and timings.

## Conventions
- Error envelope: `{ "error": { "code", "message", "request_id" } }`.
- Config comes from env vars, loaded through pydantic-settings. `.env.example` is kept up to date.
- Each phase ends with passing tests, an updated README section, and a row in `docs/ai-workflow/log.md`.
