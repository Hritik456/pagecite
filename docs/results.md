# Results

Measured results for PageCite (chaos tests, DLQ demo; P5 adds eval and load).

## Chaos: embed worker killed mid-run (2026-11-24 17:54 UTC)

- Commit `ad682c0` (+ uncommitted changes); `scripts/chaos_embed.py --runs 3 --mode container`, 50 PDFs per run, real OpenAI embeddings.
- Worker config: APP_ENV=chaos, visibility timeout 120 s, soft/hard time limit 60/90 s, retry backoff max 30 s, embed batch 4, restore scan every 10 s (unacked_restore_throttle=1).

| Run | Kill | Docs | INDEXED | FAILED | Chunks | Embed tasks running at kill | Redelivered | Max attempts | Recovery after restart | Duplicate chunks | Redis = MySQL | Non-active keys | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | container | 50 | 50 | 0 | 679 | 1 | 2 | 2 | 110 s | 0 | yes | 0 | PASS |
| 2 | container | 50 | 50 | 0 | 683 | 2 | 2 | 2 | 121 s | 0 | yes | 0 | PASS |
| 3 | container | 50 | 50 | 0 | 668 | 1 | 2 | 2 | 108 s | 0 | yes | 0 | PASS |

**3/3 runs passed.**

## DLQ + replay demo (2026-11-24 17:56 UTC)

- Commit `ad682c0`; `scripts/smoke_dlq.py` on the chaos overlay (FAULT_EMBED_429_PREFIX=fail-, retry backoff max 30 s, max retries 5).
- `fail-dlq-demo.pdf` (document 255): version 1 gets HTTP 429 with Retry-After: 1 on every embed call.

```
t+  0.6s  document UPLOADED; embed attempts=None stage=None
t+  1.6s  document PARSING; embed attempts=None stage=None
t+  3.7s  document CHUNKING; embed attempts=None stage=None
t+  4.2s  document RETRY_WAIT; embed attempts=1 stage=RETRY_WAIT (RATE_LIMITED: upstream rate limit (429))
t+  5.2s  document RETRY_WAIT; embed attempts=2 stage=RETRY_WAIT (RATE_LIMITED: upstream rate limit (429))
t+  6.2s  document RETRY_WAIT; embed attempts=3 stage=RETRY_WAIT (RATE_LIMITED: upstream rate limit (429))
t+  7.2s  document RETRY_WAIT; embed attempts=4 stage=RETRY_WAIT (RATE_LIMITED: upstream rate limit (429))
t+  9.2s  document RETRY_WAIT; embed attempts=5 stage=RETRY_WAIT (RATE_LIMITED: upstream rate limit (429))
t+ 24.0s  document FAILED; embed attempts=6 stage=FAILED (RATE_LIMITED: upstream rate limit (429))
t+ 24.7s  POST /documents/255/replay -> 202 version=2
t+ 27.7s  document INDEXED; processing_version=2 active_version=2
```

- [x] v1 FAILED after its retries (('FAILED', 6, 'FAILED'))
- [x] 1 try + 5 retries (6 attempts) (attempts=6)
- [x] in the DLQ: kind=exhausted, RATE_LIMITED ({"createdAt": "2026-11-24T17:55:42.286703", "documentId": 255, "error": "RATE_LIMITED: upstream rate limit (429)", "errorCode": "RATE_LIMITED", "filename": "fail-dlq-demo.pdf", "id": 1, "kind": "exhausted", "replayedAt": null, "stage": "embed", "version": 1})
- [x] replay ends INDEXED as version 2 (INDEXED v2 active=2)
- [x] Redis holds only v2 keys (versions=[2] keys=5)
- [x] DLQ row marked replayed
- [x] /ask cites the replayed document (cited=[255] answer='This provision is called “the cobalt heron allowance” in the application procedu')

**PASS: 7/7 checks.**
