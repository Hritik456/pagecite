# ADR 0002: The Redis index is derived from MySQL

- Status: accepted (P2, 2026-11-24)
- Deciders: project owner, with Claude Code

## Context

Retrieval (P4) runs `FT.HYBRID` over a Redis 8.4 Query Engine index: one hash per chunk with `tenant` TAG, `doc_id` TAG, `text` TEXT, `page` NUMERIC and `embedding` VECTOR. Redis is memory-first, so if it loses data (eviction, a flush, a lost volume) the index must come back without re-running the pipeline. `docs/specs.md` §5 makes MySQL the truth for chunk text but has no column for the embedding, and no table for the parse stage's output.

## Decision

1. **MySQL is the single source of truth. The index is a projection of it.** Nothing exists only in Redis.
2. **Migration V2** adds what the spec table is missing:
   - `chunks.embedding BLOB` (little-endian float32, the exact bytes Redis stores) and `chunks.embedding_model`. Embeddings from an API aren't bit-for-bit reproducible, so re-embedding could never rebuild an *identical* index, and it would cost money every time.
   - `document_pages(document_id, version, page_no, text, ocr)`, UNIQUE(document_id, version, page_no): the parse stage's output, which the chunk stage reads. As a table it follows the same "upsert on a UNIQUE key in the same transaction" rule as every other stage.
3. **`flask --app apps.api reindex --tenant <id> | --all`** streams the current-version chunks of INDEXED documents from MySQL (500 per round trip) and HSETs them through the same `write_chunks()` the index worker uses. It first deletes the target's keys, so stale keys can't survive. `--all` exists because `FLUSHDB` clears the index for *every* tenant, and restoring only one tenant would silently lose the rest.
4. The index definition is created when a worker starts, and before any write (`ensure_index`, which is idempotent).

## Idempotency detail: `processed_jobs` is inserted last

CLAUDE.md says "insert `processed_jobs(job_key)` first and exit if it is a duplicate". We split that rule into two parts:

- **The early exit** happens first: `begin_stage` reads `processed_jobs`, and if the key exists the task re-enqueues the next stage and stops.
- **The insert** happens at the **end** of the output transaction, after the output upserts.

Why not insert it first? The embed stage commits in batches (one transaction per 100 vectors), so the stage's output doesn't live in one transaction. If the key were inserted first and committed, a crash after batch 1 would leave the job marked done with most of the vectors missing, and the redelivered task would skip it for good. Inserting it last means the key exists only once the whole output exists.

The cost: two copies of a task running at the same time (a redelivery racing the original) can both do the work. That wastes effort but can't corrupt output:
- every write is an upsert on a UNIQUE key, and both copies compute the same rows
- the second `processed_jobs` insert hits the primary key; that copy rolls back its transaction and reports `duplicate`
- `tests/db/test_pipeline.py::test_concurrent_duplicate_that_finishes_first_wins` covers this

Related: a redelivered task that finds its key **re-enqueues the next stage** instead of exiting silently. The first run may have died between its commit and its enqueue, and every stage is idempotent, so the extra message is harmless.

## Consequences

- Rebuilding the index costs nothing in API calls and is byte-identical. `scripts/smoke_ingest.py --reindex-check` verifies this by fingerprinting all tenants before `FLUSHDB` and after `reindex --all`.
- MySQL storage grows by about 6 KB per chunk (1536 × 4 bytes). For the spec's 40k-chunk profile that is about 246 MB, which is acceptable.
- Changing the embedding model or dimension means re-embedding (P3's replay creates a new version). `embedding_model` records which model produced each vector.
- If a re-chunk changes a chunk's text, the chunk upsert clears that row's stored embedding, so an embedding never describes different text.
