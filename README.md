# PageCite

PageCite answers questions from piles of PDF and DOCX files (policies, circulars, manuals) and **cites the page** each answer comes from. It is built as an async RAG platform. Files are uploaded straight to S3. Staged workers then parse, chunk, embed and index them. Answers use hybrid retrieval with citations and are checked by an eval harness.

> Status: **P1 (upload pipeline)** and **P2 (staged workers → Redis vector + text index)** are done. Reliability (P3), retrieval (P4) and eval/deploy (P5) are next. The full design is in [docs/specs.md](docs/specs.md), and the decisions are in [docs/adr/](docs/adr/).

## Problem

A naive RAG demo has four problems:

1. It parses inside the HTTP request, so large uploads time out.
2. It loses work when a worker crashes.
3. Vector-only search misses exact terms such as circular numbers, section IDs and names.
4. No one can tell whether its answers are correct.

PageCite fixes these with direct-to-S3 uploads, durable staged workers that retry and can be replayed, hybrid retrieval with citations, and an evaluation harness.

## Architecture (HLD)

```mermaid
flowchart TB
  W[React console\nupload, progress, chat] -->|presigned PUT| S3[(S3 / MinIO)]
  W --> C[Caddy] --> API[Flask API]
  API --> M[(MySQL 8.4\nbatches, docs, stages,\nchunks = truth)]
  API -->|enqueue via outbox| B[(Redis: Celery broker)]
  B --> P[parse workers\nCPU pool] --> K[chunk workers] --> E[embed workers\nrate-limited] --> I[index workers]
  P & K & E & I --> M
  I --> RX[(Redis 8.4 Query Engine\nVECTOR + TEXT index)]
  API -->|FT.HYBRID + rerank| RX
  API -->|answer, rerank, embed| O[OpenAI]
  P & K & E & I -->|retries exhausted| DLQ[dead_letters + replay API]
```

Stack: Python 3.11, Flask + SQLAlchemy 2, MySQL 8.4 + Flyway, Redis 8.4, S3 (MinIO locally), React + TypeScript + Vite + Tailwind, Docker Compose + Caddy.

## Upload flow (P1)

```mermaid
sequenceDiagram
  autonumber
  participant B as Browser
  participant A as API (via Caddy /api)
  participant DB as MySQL
  participant S as S3 / MinIO

  B->>B: SHA-256 of each file (WebCrypto)
  B->>A: POST /batches {files:[{name,size,sha256,mime}]}
  A->>A: quota checks (file size, files per batch, PDF/DOCX only)
  A->>DB: batch UPLOADING + documents CREATED (one transaction)
  A-->>B: {batchId, uploads:[{documentId, presignedUrl, headers}]}
  B->>S: PUT presignedUrl (Content-Type + x-amz-checksum-sha256 are signed)
  S->>S: reject if bytes ≠ declared SHA-256
  B->>A: POST /documents/:id/complete
  A->>S: HEAD object (ChecksumMode=ENABLED)
  A->>A: tenant prefix, size and checksum match the DB row?
  A->>DB: one transaction: UPDATE … SET UPLOADED WHERE status='CREATED'<br/>+ outbox_events row (only if the update hit 1 row)
  A-->>B: {document:{status:"UPLOADED"}}
```

Design points:

- **The API never touches file bytes.** Browsers upload directly to S3 with a presigned PUT that expires after 15 minutes. Object keys look like `tenant/{tenantId}/batch/{batchId}/{documentId}/{safeFilename}`.
- **Integrity is enforced by S3, not trusted from the client.** The declared SHA-256 is signed into the URL as `x-amz-checksum-sha256`, so S3/MinIO rejects any other bytes. That also pins the size. `complete` re-checks the size and checksum with `HEAD`.
- **Quotas run before any upload.** A file over the tenant's `max_file_mb`, too many files, or a type other than PDF/DOCX is rejected at `POST /batches`. No URL is issued and no rows are written.
- **`complete` is idempotent.** A conditional `UPDATE … WHERE status='CREATED'` and the outbox insert share one transaction, so a duplicate or concurrent `complete` returns 200 and writes no second outbox row. The batch row is locked first, so completes within one batch are serialised in a fixed lock order. When no document is still CREATED, the batch moves to PROCESSING.
- **Tenant isolation.** A document is loaded by id *and* tenant, so another tenant's document returns 404. Its key must also start with `tenant/{id}/`.
- **Auth** uses a static per-tenant key in `X-Tenant-Key`. Only its SHA-256 is stored (`tenants.api_key_hash`).
- **Errors** always use `{ "error": { "code", "message", "request_id" } }`. Logs hold ids, sizes and timings, never filenames or content.

### API (P1)

| Route | Notes |
|---|---|
| `POST /batches` | body `{files:[{name,size,sha256,mime}]}` → 201 `{batchId, uploads:[{documentId, presignedUrl, headers}]}`; 413 `QUOTA_EXCEEDED`, 415 `UNSUPPORTED_TYPE`, 422 `VALIDATION_ERROR` |
| `POST /documents/:id/complete` | 200 `{document}` (also for duplicates); 404 `NOT_FOUND`, 409 `NOT_UPLOADED` / `OBJECT_KEY_MISMATCH`, 422 `SIZE_MISMATCH` / `CHECKSUM_MISMATCH` |
| `GET /batches/:id` · `GET /documents/:id` | progress: the batch with its documents, each with its per-stage status (`parse`/`chunk`/`embed`/`index`: status, attempts, timings); the document with its stages |
| `GET /health` | no auth |

All routes except `/health` need `X-Tenant-Key`. Through Caddy they are served under `/api`.

## Pipeline (P2)

```mermaid
flowchart LR
  C[complete] -->|same tx| OB[(outbox_events)]
  OB -->|relay: FOR UPDATE SKIP LOCKED| Q1[[parse queue]]
  Q1 --> P[parse<br/>pdfplumber / python-docx<br/>Tesseract for scanned pages] -->|document_pages| Q2[[chunk queue]]
  Q2 --> K[chunk<br/>~500 tokens, 50 overlap] -->|chunks| Q3[[embed queue]]
  Q3 --> E[embed<br/>batches of 100] -->|chunks.embedding| Q4[[index queue]]
  Q4 --> I[index] -->|HSET chunk:tenant:id| RX[(Redis index)]
  I --> D[document INDEXED<br/>batch DONE / PARTIAL]
```

| Stage | Reads | Writes (upsert on a UNIQUE key) | Document status |
|---|---|---|---|
| **parse** | the S3 object | `document_pages` (document, version, page_no): text per page; a page with almost no text layer is OCR'd | PARSING |
| **chunk** | `document_pages` | `chunks` (document, version, ord): ~500 tokens, ~50 tokens overlap, headings start a chunk, `page_from`/`page_to` | CHUNKING |
| **embed** | chunks without an embedding | `chunks.embedding` (float32) + `embedding_done`, 100 per OpenAI call, committed per batch | EMBEDDING |
| **index** | the document's chunks | one Redis hash per chunk: `tenant` TAG, `doc_id` TAG, `text` TEXT, `page` NUMERIC, `embedding` VECTOR (HNSW, FLOAT32, 1536, COSINE) | INDEXED |

**Why separate queues.** Parsing and OCR are CPU-bound and slow. Embedding is network-bound and rate-limited by OpenAI. Indexing is fast. One queue per stage, each with its own worker service, means a 200-page scan never blocks embedding, and each pool is sized for its own bottleneck: `-c 2` on `parse` for CPU, `-c 2` on `embed` as the cap on parallel API calls. Stages chain by enqueueing the next stage at the end of each task, not as one large Celery chain, so every hop is a durable message ([ADR 0003](docs/adr/0003-stage-queues.md)).

**Idempotency** (the invariants in [CLAUDE.md](CLAUDE.md)):
- Every task runs with `acks_late=True` and `task_reject_on_worker_lost=True`. A worker that dies mid-task doesn't ack, so the broker redelivers the task.
- Each stage's output transaction upserts its rows on their UNIQUE key and inserts `processed_jobs(doc:{id}:{stage}:v{n})` **last** ([ADR 0002](docs/adr/0002-redis-index-derived-from-mysql.md) explains why last, not first).
  - A redelivered task that finds its key **re-enqueues the next stage** and stops. This covers a crash between the commit and the enqueue, so the pipeline can't stall.
  - If two copies race, the one that loses the `processed_jobs` insert rolls back its identical output.
- Document status only moves forward (`UPDATE … WHERE status IN (allowed predecessors)`), so a late duplicate never rewinds a document.
- Spec flow 2, a worker crash mid-embed: batch 1's rows are committed with `embedding_done=1`, and the redelivered task embeds only the remaining rows. No duplicate chunks, and no second payment for vectors already stored.

**Failures (P2).** A `PermanentError` (corrupt or encrypted PDF, unsupported type, no text, OCR limits) fails the stage and the document immediately, with no retry. The batch ends `PARTIAL`. Retries with backoff, the DLQ and replay are P3.

**Time bounds.** The broker's visibility timeout is 30 min, so a SIGKILLed worker's task returns within 30 min. Every task has `soft_time_limit=1200 s`. OCR is capped at `OCR_MAX_PAGES=50` scanned pages (checked before any OCR runs), 60 s per page and a 900 s budget, so no task outlives the timeout ([ADR 0003](docs/adr/0003-stage-queues.md)).

**The index is derived.** MySQL holds the chunk text **and** the embedding (V2), so `flask --app apps.api reindex --tenant <id>` or `--all` rebuilds Redis byte-for-byte with no OpenAI calls. It's the answer to "what if Redis loses data?".

**DOCX pages** come from explicit and last-rendered page breaks, so they are approximate. DOCX has no fixed pagination; PDF pages are exact.

## Run locally

Requirements: Docker (Compose v2), Node 20+, and [uv](https://docs.astral.sh/uv/) (it installs Python 3.11 for the project).

1. Create `.env`. The keys are listed in [.env.example](.env.example). These values are for local development only:

   ```env
   APP_ENV=development
   LOG_LEVEL=INFO
   SECRET_KEY=dev-only-not-secret
   MYSQL_HOST=localhost
   MYSQL_PORT=3306
   MYSQL_DATABASE=pagecite
   MYSQL_USER=pagecite
   MYSQL_PASSWORD=pagecite-dev
   MYSQL_ROOT_PASSWORD=root-dev
   DATABASE_URL=mysql+pymysql://pagecite:pagecite-dev@localhost:3306/pagecite
   TEST_DATABASE_URL=mysql+pymysql://pagecite:pagecite-dev@localhost:3306/pagecite_test
   REDIS_URL=redis://localhost:6379/0
   CELERY_BROKER_URL=redis://localhost:6379/1
   S3_ENDPOINT_URL=http://localhost:9000
   S3_PUBLIC_ENDPOINT_URL=http://localhost:9000
   S3_BUCKET=pagecite
   S3_TEST_BUCKET=pagecite-test
   AWS_REGION=us-east-1
   AWS_ACCESS_KEY_ID=pagecite
   AWS_SECRET_ACCESS_KEY=pagecite-dev-secret
   MINIO_ROOT_USER=pagecite
   MINIO_ROOT_PASSWORD=pagecite-dev-secret
   PRESIGN_EXPIRES_SECONDS=900
   CADDY_HTTP_PORT=8000
   OPENAI_API_KEY=<your key>
   OPENAI_EMBED_MODEL=text-embedding-3-small
   ```

2. Build the web console and start everything:

   ```sh
   npm --prefix apps/web ci
   npm --prefix apps/web run build          # Caddy serves apps/web/dist
   docker compose up -d --build             # mysql, redis, flyway, minio (+ buckets), api, caddy,
                                            # relay + worker-parse/chunk/embed/index
   ```

3. Create a tenant. The key is printed once:

   ```sh
   docker compose exec api flask --app apps.api seed-tenant --name demo --max-file-mb 5 --max-batch-files 20
   ```

4. Open <http://localhost:8000>, paste the key, pick files (for example everything in [samples/](samples/)) and upload. Each row shows parse → chunk → embed → index live. MinIO's console is at <http://localhost:9001>.

5. Smoke-test the whole pipeline on the running stack. It seeds a throwaway tenant, uploads `samples/` (4 text PDFs + 1 scanned), waits for INDEXED, searches every known phrase, compares chunk counts in MySQL and Redis, and with `--reindex-check` flushes Redis and rebuilds it:

   ```sh
   uv run python scripts/smoke_ingest.py --reindex-check
   docker compose exec api flask --app apps.api reindex --all      # or --tenant <id>
   ```

   `scripts/make_samples.py` regenerates `samples/` deterministically.

For UI development, run `npm --prefix apps/web run dev` (port 5174). It proxies `/api` to Caddy.

Notes:
- MinIO no longer publishes community Docker images, so Compose uses Chainguard's from-source MinIO build (`cgr.dev/chainguard/minio`), pinned by digest.
- The API reaches MinIO at `http://minio:9000`. Presigned URLs are signed for `S3_PUBLIC_ENDPOINT_URL` (the host the browser uses), because the host is part of the signature.

## Testing

```sh
uv sync                                                   # .venv with Python 3.11 + dev deps
docker compose up -d mysql flyway-test minio minio-init redis   # what the tests need
uv run pytest -q                  # unit + DB tests (integration excluded by default)
uv run pytest -m integration      # real MinIO + Redis tests
```

| Layer | Folder | Needs | Storage / externals |
|---|---|---|---|
| Pure logic: object keys, filename sanitising, quota rules, schema validation; parsing (sample PDFs, encrypted/corrupt, OCR routing and caps, DOCX pages), chunking (page mapping, headings, overlap, size bounds), float32 encoding | `tests/unit` | nothing (no Docker) | none; the real-Tesseract test runs only if `tesseract` is on PATH |
| API + DB: quotas end to end, duplicate/concurrent `complete`, tenant isolation, auth, error envelope, CLI; **pipeline**: relay + SKIP LOCKED, a stage run twice, a redo without `processed_jobs`, a racing duplicate, crash mid-embed, permanent error → FAILED with no retry, batch DONE/PARTIAL, stage progress in the API | `tests/db` | compose MySQL | `FakeStorage`, `FakeEmbedder`, recorded enqueues |
| End to end: presign → real PUT → complete; MinIO rejects tampered bytes, oversize bodies and missing signed headers; **the 5 samples upload → relay → parse → chunk → embed → index** with eager Celery; FT.SEARCH finds every known phrase in the right document and page; MySQL count = Redis count; `reindex` (one tenant and `--all`) restores an identical fingerprint | `tests/integration` | MySQL + MinIO + Redis | real MinIO (bucket `pagecite-test`), real Redis under `idx:test_chunks` / `test:chunk:`, `FakeEmbedder` (no OpenAI cost), Tesseract if installed |

The Done-when run on real queues, acks, OCR and OpenAI is `scripts/smoke_ingest.py` (see Run locally).

- DB tests run against a separate database, **`pagecite_test`**. It is created on MySQL's first start by `db/init/01-test-db.sh` and migrated by the `flyway-test` service with the same Flyway migrations as production. There is no SQLite: V1 is MySQL DDL, and later phases rely on `ON DUPLICATE KEY UPDATE` and `SKIP LOCKED`. Tables are emptied before each test.
- Integration tests use their own bucket and empty it before each test.
- If a dependency isn't running, the tests fail at once with the `docker compose up` command to run.
