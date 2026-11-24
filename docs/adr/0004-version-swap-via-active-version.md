# ADR 0004: Version swap for retrieval via `documents.active_version`

- Status: accepted (P3, 2026-11-24)
- Deciders: project owner, with Claude Code

## Context

`POST /documents/:id/replay` reprocesses a document as a new `processing_version`. History is never overwritten (CLAUDE.md), and P3 requires that "the old version's chunks stay until the new version is INDEXED". P4 retrieval is already live: `/ask` reads Redis hits, then loads chunk text from MySQL. During a replay:

- the old version must keep answering while the new one is parsed, chunked and embedded;
- `/ask` must never return chunks from **both** versions (duplicate or stale blocks);
- a new version that fails or is cancelled after writing its Redis keys must not leave them behind.

Redis can't do the switch by itself:
- The keys (`chunk:{tenant}:{chunkId}`) carry no version that a query could filter on without a join.
- A MULTI/EXEC of "HSET new keys + DEL old keys" isn't a reliable snapshot, because Query Engine reads can run on background threads between those commands.

## Decision

1. **`documents.active_version`** (migration V3) is the single version retrieval serves.
   - Replay bumps `processing_version` and leaves `active_version` alone.
   - The index stage flips it **in the same UPDATE that marks the new version INDEXED**:
     `UPDATE documents SET status='INDEXED', active_version=:v WHERE id=? AND processing_version=:v AND status='EMBEDDING'`.
2. **Retrieval joins on it.** `load_candidates()` (P4) already reloads every candidate chunk from MySQL by id and tenant; it now also requires `chunks.version = documents.active_version`.
   - One SELECT is one consistent snapshot, so each document contributes chunks from exactly one version: before the commit only the old version, after it only the new one.
   - Redis hits for the inactive version are dropped and counted (`stale_hits` in the token log).
3. **Redis is cleaned after the commit:**
   - **After a swap:** the index task's `after` hook deletes every non-active version's keys (chunk ids from MySQL). It also runs when the index task is redelivered as a duplicate.
   - **Orphans:** after *any* terminal transition of a version that did not become active (FAILED through the DLQ, or CANCELLED, e.g. after its HSET), that version's keys are deleted.
   - Rule: *after any terminal transition of version v, if v ≠ `active_version`, delete v's keys*. The chaos harness checks it as an invariant: 0 keys for non-active versions.
4. **`reindex` writes only active versions** (`indexable_chunks_query` joins on `active_version`, not on `status='INDEXED'`), so a reindex during a replay keeps the still-active old version.

## Consequences

- No window ever serves two versions of a document; the test hooks into the index task between its HSET and its commit, and again between the commit and the cleanup.
- For the milliseconds between the commit and the cleanup, stale keys can take some of the top-20 candidate slots, slightly shrinking the candidate set. This is visible in the log.
- This is a **consistency** join on the truth read, not a security filter. Tenant isolation is unchanged: `@tenant` stays inside every index query (CLAUDE.md invariant), and the version join never decides tenancy.
- Old versions' `document_pages`/`chunks` rows stay in MySQL (history). Only their Redis keys go. A future retention job could prune them.
