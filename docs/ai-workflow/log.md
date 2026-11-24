# AI Workflow Log

After each phase, fill one row. The token and cost numbers come from `/cost` in Claude Code at the end of the session.

| Phase | Date | Sessions | Tokens (in / out) | Cost | Plan changes I made | Bugs I caught in review | Done-when passed? |
|---|---|---|---|---|---|---|---|
| P1 | 2026-11-24 | 1 | | | Spec gaps settled before coding: SHA-256 via native S3 checksum (not user metadata); `tenants.api_key_hash` added to V1; BIGINT ids; DB tests on a real MySQL test DB with the Flyway schema (no SQLite), MinIO only in `-m integration` | | pytest -q ✅ (62), pytest -m integration ✅ (5), HTTP replay of the UI flow ✅; browser check by hand: pending |
| P2 | 2026-11-24 | 1 | | | Before coding: embeddings stored in MySQL (V2) so reindex is identical; `document_pages` table for parse output; integration in-process + a `smoke_ingest.py` Done-when run on the real stack; generated, committed samples. On plan review: visibility_timeout 7200 s → 1800 s plus time limits and OCR caps; `reindex --all` so FLUSHDB can't lose other tenants; ADR note on why `processed_jobs` is inserted last | | pytest -q ✅ (116), pytest -m integration ✅ (13), smoke_ingest ✅ 13/13 (5/5 INDEXED incl. OCR, phrases found, MySQL = Redis), --reindex-check ✅ 17/17 (identical fingerprint, 3 tenants); repo public: pending (your step) |
| P3 | | | | | | | |
| P4 | | | | | | | |
| P5 | | | | | | | |
| P6 | | | | | | | |

## Notes per phase

<!-- What surprised you, what you'd explain in an interview, prompt changes that helped -->

### P1
- Model: Opus for both planning and implementation (single session).
- MinIO no longer publishes community images (Docker Hub and Quay both gone). Switched to Chainguard's from-source MinIO build, pinned by digest. It is still MinIO, so no ADR was needed.
- Compose split a string `command:` at the `&` in the Flyway JDBC URL. Flyway then printed its usage text and **exited 0**: a silent failure. Fixed with the exec-form list.
- MinIO's `/minio/health/ready` returns 400 without a `Host` header (raw HTTP/1.0 probe).
- A probe confirmed that boto3 signs `content-type` and `x-amz-checksum-sha256` into the presigned URL. MinIO rejects wrong bytes (`XAmzContentChecksumMismatch`), a larger body, a missing checksum header and a wrong content type.
- Interview point: the checksum in the URL means S3 enforces integrity and size. The API never sees the bytes and doesn't have to trust the client.
- Interview point: `complete` locks the batch row first, so concurrent completes in one batch can't deadlock on the "all uploaded → PROCESSING" check. Covered by a threaded test.
- DB test setup went from ~3 s to ~0.1 s per test by switching TRUNCATE (recreates tables) to DELETE.

### P2
- Model: Opus for planning and implementation (single session).
- Tests caught a production bug: task modules were registered only in worker processes (`include=` loads at worker start), so the **relay**, a pure producer, would have failed with `NotRegistered` on its first enqueue. Fixed by importing the task modules whenever the Celery app is imported.
- redis-py 8 defaults to RESP3, where `FT.SEARCH` returns a map instead of a list. The client is pinned to `protocol=2`.
- The first `_upsert_stage` draft reused the UPDATE values for the INSERT, which would have put `attempts = attempts + 1` into the INSERT values. Split into insert and update values.
- Possible deadlock found in design: the index stage locks the document and then the batch, while `complete` locks the batch and then the documents. Every transaction that touches the batch now locks the batch row first.
- Turning the workers on drained the outbox backlog from the P1 browser check. Five ~2 KB non-PDFs failed `CORRUPT` with no retry (correct), and the real PDFs were INDEXED (one produced 106 chunks).
- Tesseract was already installed on the Windows host (5.5), so the integration tests use real OCR there. The stub is only a fallback.
- Interview point: "exactly-once effect" = at-least-once delivery (acks_late + outbox) + idempotent stages (upsert on a UNIQUE key + `processed_jobs`) + forward-only status updates + "duplicate → re-enqueue next stage".
- Interview point: the index is a projection. FLUSHDB → `reindex --all` → byte-identical fingerprint for every tenant, with no OpenAI calls, because the vectors live in MySQL.
- Smoke run: 5 samples went UPLOADED → INDEXED in about 13 s on the local stack (`-c 2` per queue).
- Plan review: visibility_timeout 7200 s → 1800 s + task time limits + OCR caps (a killed worker would stall a doc for 2 h).
- Plan review: FLUSHDB + single-tenant reindex would wipe other tenants' index → added reindex --all.
- Tests caught: relay process didn't register tasks (NotRegistered in prod); redis-py 8 RESP3 reply shape; lock-order deadlock (batch row now always locked first).
## Totals (fill at the end, for README and interviews)

- Total sessions:
- Total tokens / cost:
- Prompt changes that reduced rework:
