# AI Workflow Log

After each phase, fill one row. The token and cost numbers come from `/cost` in Claude Code at the end of the session.

| Phase | Date | Sessions | Tokens (in / out) | Cost | Plan changes I made | Bugs I caught in review | Done-when passed? |
|---|---|---|---|---|---|---|---|
| P1 | 2026-11-24 | 1 | | | Spec gaps settled before coding: SHA-256 via native S3 checksum (not user metadata); `tenants.api_key_hash` added to V1; BIGINT ids; DB tests on a real MySQL test DB with the Flyway schema (no SQLite), MinIO only in `-m integration` | | pytest -q ✅ (62), pytest -m integration ✅ (5), HTTP replay of the UI flow ✅; browser check by hand: pending |
| P2 | 2026-11-24 | 1 | | | Before coding: embeddings stored in MySQL (V2) so reindex is identical; `document_pages` table for parse output; integration in-process + a `smoke_ingest.py` Done-when run on the real stack; generated, committed samples. On plan review: visibility_timeout 7200 s → 1800 s plus time limits and OCR caps; `reindex --all` so FLUSHDB can't lose other tenants; ADR note on why `processed_jobs` is inserted last | | pytest -q ✅ (116), pytest -m integration ✅ (13), smoke_ingest ✅ 13/13 (5/5 INDEXED incl. OCR, phrases found, MySQL = Redis), --reindex-check ✅ 17/17 (identical fingerprint, 3 tenants); repo public: pending (your step) |
| P3 | 2026-11-24 | 1 | | | Before coding: every final failure gets a DLQ row; replay from FAILED/INDEXED/CANCELLED; fault injection through an env-gated `FaultyEmbedder` in context.py, refused in production (with a test); §8 limits (page cap, parse mem_limit, attempt cap). Explicit design answers: chaos overlay with VT 120 s (production 1800 s) + restore throttle 1; `active_version` for the atomic swap. On plan review: attempts committed before the handler (crash-count test); orphan Redis keys deleted after any non-active terminal version (+ chaos invariant) | | pytest -q ✅ (220), pytest -m integration ✅ (36), chaos ✅ 3/3 runs (9/9 invariants each, real mid-task kills), smoke_dlq ✅ 7/7 (429 → 6 attempts → DLQ → replay → v2 INDEXED) |
| P4 | 2026-11-24 | 1 | | | Before coding: FT.HYBRID via raw command + tested parser (not the experimental helper); a synthetic FIAS circulars corpus (fictional org, "Synthetic sample" headers) whose near-twin IDs show hybrid > vector; citations event = only the cited blocks. On plan review: release the DB connection before the SSE stream (+ tests), explicit OpenAI timeouts, a prompt-injection guard + injected page and question. questions.json reviewed against the PDF (added "mandatory", "16:00") | | pytest -q ✅ (167), pytest -m integration ✅ (29), smoke_ask ✅ 31/31 (5 cited answers on the right pages, off-topic → exact abstain, injection not followed, token lines per call) |
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

### P4
- Model: Opus for planning and implementation (single session); the app uses gpt-5.4-nano (decompose, rerank) and gpt-5.4-mini (answer).
- Probes before coding: `FT.HYBRID` (since Redis 8.4.4) works with `@tenant` inside both SEARCH and VSIM `FILTER` (another tenant → 0 rows); `LOAD @__key` gives chunk ids; `YIELD_SCORE_AS` is accepted on SEARCH and COMBINE, but not on VSIM before `FILTER`.
- RediSearch ANDs bare terms, so a natural-language question in the text branch would almost never match. It is tokenised, the terms OR'd, and identifiers added as exact phrases.
- The OpenAI SDK (3.58) no longer ships `httpx`; timeouts use `openai.Timeout`. The gpt-5.4 models reject `reasoning_effort="minimal"` (only none/low/medium/high), and their default is no reasoning tokens.
- A UI bug caught in writing: a global regex with `.test()` keeps `lastIndex`, which would skip every other `[n]` chip.
- Real run: first token about 3.5–4.7 s. Decompose costs about 1–1.7 s; rerank is the largest token cost (about 4k prompt tokens for 20 passages × 200 tokens). Both are worth measuring in P5 (`hybrid` vs `hybrid_rerank`, ± decompose).
- The off-topic question still pays for rerank + answer, because the tenant has documents and the model is the one that abstains. The zero-candidate path (no LLM call) applies only to tenants with nothing indexed.
- Interview point: the injected "SYSTEM NOTE TO AI ASSISTANTS … 50% bonus" page was retrieved and given to the model, and the answer still gave the real hours, cited. The defence is fencing + an untrusted-data rule + ids-only rerank output + citations checked against block numbers.
- Interview point: a streaming answer holds 0 pooled DB connections, proven by a test that is mid-stream in 18 requests at once against a 15-connection pool.
- Plan review: DB connection was held during LLM streaming → released before first token (pool exhaustion); added timeouts, no retry on streams.
- Plan review: added prompt-injection guard + an injected test page; verified the answer ignores it.
- Eval review: q5/q7 checked only half the question → added "mandatory" and "16:00".
### P3
- Model: Opus for planning and implementation (single session).
- Read kombu/celery source before designing:
  - a killed **pool child** is requeued at once (`reject_on_worker_lost`)
  - a killed **container** waits for the visibility timeout, and kombu's restore scan is throttled to every 10th tick (about 100 s), so `unacked_restore_throttle=1` brings it to VT + 10 s
  - ETA retries are held unacked, so `retry_backoff_max` must stay below VT
  - eager mode re-runs `task.retry()` synchronously (but only with `task_eager_propagates=False`)
- **The first chaos run was invalid and the harness caught it.** Embed tasks took about 0.7 s, so the two "running" tasks had finished before `docker kill` landed, and the "≥ 1 redelivered" invariant would have failed. The fix: longer documents (about 13 chunks, 4 embed calls), the container id resolved up front, and the kill triggered on a task that started < 0.5 s ago. After that, every run had real redeliveries.
- Chaos timeline matches the design: 48/50 documents done by about 68 s, the last two (killed in flight) at about 126 s = VT 120 + scan.
- Bugs caught by tests while building:
  - a malformed `Retry-After` crashed the classifier (it's now ignored)
  - the SQL `:v` inside a `text()` LIKE pattern was read as a bind parameter (chaos script)
- The P4 concurrency test (18 streams vs a 15-connection pool) failed once under heavy host load: one request lost its MySQL connection (`2013`) through Docker Desktop's port forwarding, with no server-side errors (`Max_used_connections` 30 of 151, no restarts). It passed 7/7 reruns. Recorded as an environment flake, not hidden.
- Interview point: the version swap is one MySQL UPDATE (`status='INDEXED', active_version=v`) plus a join in the retrieval read, so no instant serves two versions. Redis cleanup is best-effort *after* the switch, and the chaos run checks "0 keys for non-active versions".
- Interview point: retries honour Retry-After but are capped below the visibility timeout. The cap is a deliberate trade-off: an ETA longer than VT would duplicate the task.
- Plan review: attempt counter must commit before the handler, or an OOM-looping poison PDF never reaches the cap.
- Plan review: orphan Redis keys when v2 fails/cancels after HSET → general "delete non-active version keys" rule + chaos invariant.
- Plan review: chaos test vs 30-min visibility timeout → chaos override (120 s) with a production guard.
- Harness caught: first chaos run killed nothing in flight (tasks too fast); made it fail instead of silently passing.
## Totals (fill at the end, for README and interviews)

- Total sessions:
- Total tokens / cost:
- Prompt changes that reduced rework:
