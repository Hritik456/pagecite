# AI Workflow Log

After each phase, fill one row. The token and cost numbers come from `/cost` in Claude Code at the end of the session.

| Phase | Date | Sessions | Tokens (in / out) | Cost | Plan changes I made | Bugs I caught in review | Done-when passed? |
|---|---|---|---|---|---|---|---|
| P1 | 2026-11-24 | 1 | | | Spec gaps settled before coding: SHA-256 via native S3 checksum (not user metadata); `tenants.api_key_hash` added to V1; BIGINT ids; DB tests on a real MySQL test DB with the Flyway schema (no SQLite), MinIO only in `-m integration` | | pytest -q ✅ (62), pytest -m integration ✅ (5), HTTP replay of the UI flow ✅; browser check by hand: pending |
| P2 | | | | | | | |
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

## Totals (fill at the end, for README and interviews)

- Total sessions:
- Total tokens / cost:
- Prompt changes that reduced rework:
