# PageCite

PageCite answers questions from piles of PDF and DOCX files (policies, circulars, manuals) and **cites the page** each answer comes from. It is built as an async RAG platform. Files are uploaded straight to S3. Staged workers then parse, chunk, embed and index them. Answers use hybrid retrieval with citations and are checked by an eval harness.

> Status: **P1, the upload pipeline**, is done. Workers (P2), reliability (P3), retrieval (P4) and eval/deploy (P5) are next. The full design is in [docs/specs.md](docs/specs.md).

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
| `GET /batches/:id` · `GET /documents/:id` | batch with its documents; document with its stages |
| `GET /health` | no auth |

All routes except `/health` need `X-Tenant-Key`. Through Caddy they are served under `/api`.

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
   ```

2. Build the web console and start everything:

   ```sh
   npm --prefix apps/web ci
   npm --prefix apps/web run build          # Caddy serves apps/web/dist
   docker compose up -d --build             # mysql, redis, flyway, minio (+ buckets), api, caddy
   ```

3. Create a tenant. The key is printed once:

   ```sh
   docker compose exec api flask --app apps.api seed-tenant --name demo --max-file-mb 5 --max-batch-files 20
   ```

4. Open <http://localhost:8000>, paste the key, pick files and upload. MinIO's console is at <http://localhost:9001>.

For UI development, run `npm --prefix apps/web run dev` (port 5174). It proxies `/api` to Caddy.

Notes:
- MinIO no longer publishes community Docker images, so Compose uses Chainguard's from-source MinIO build (`cgr.dev/chainguard/minio`), pinned by digest.
- The API reaches MinIO at `http://minio:9000`. Presigned URLs are signed for `S3_PUBLIC_ENDPOINT_URL` (the host the browser uses), because the host is part of the signature.

## Testing

```sh
uv sync                                                   # .venv with Python 3.11 + dev deps
docker compose up -d mysql flyway-test minio minio-init   # what the tests need
uv run pytest -q                  # unit + DB tests (integration excluded by default)
uv run pytest -m integration      # real MinIO tests
```

| Layer | Folder | Needs | Storage |
|---|---|---|---|
| Pure logic: object keys, filename sanitising, quota rules, schema validation | `tests/unit` | nothing (no Docker) | none |
| API + DB: quotas end to end, duplicate/concurrent `complete`, tenant isolation, prefix mismatch, auth, error envelope, CLI | `tests/db` | compose MySQL | `FakeStorage` (enforces the same signed-header rules) |
| End to end: presign → real PUT → complete for 5 PDFs; MinIO rejects tampered bytes, oversize bodies and missing signed headers | `tests/integration` | MySQL + MinIO | real MinIO, bucket `pagecite-test` |

- DB tests run against a separate database, **`pagecite_test`**. It is created on MySQL's first start by `db/init/01-test-db.sh` and migrated by the `flyway-test` service with the same Flyway migrations as production. There is no SQLite: V1 is MySQL DDL, and later phases rely on `ON DUPLICATE KEY UPDATE` and `SKIP LOCKED`. Tables are emptied before each test.
- Integration tests use their own bucket and empty it before each test.
- If a dependency isn't running, the tests fail at once with the `docker compose up` command to run.
