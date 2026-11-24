# ADR 0003: One queue per pipeline stage, bounded task time

- Status: accepted (P2, 2026-11-24)
- Deciders: project owner, with Claude Code

## Context

Ingestion has four stages with very different costs:

| Stage | Bottleneck | Typical time (samples / real PDFs) |
|---|---|---|
| parse | CPU (pdfplumber; Tesseract on scanned pages) | 0.1–10 s, minutes for long scans |
| chunk | CPU (tokenizer), light | < 3 s |
| embed | network + OpenAI rate limits | 1–8 s per 100 chunks |
| index | Redis writes | < 1 s |

A single queue would let one long OCR job hold every worker slot, while cheap index jobs and rate-limited embed jobs wait behind it.

## Decision

1. **Four queues** (`parse`, `chunk`, `embed`, `index`), each served by its own Compose service from one image:
   - `worker-parse -c 2` (CPU-bound)
   - `worker-chunk -c 2`
   - `worker-embed -c 2` (this concurrency is the cap on parallel OpenAI calls)
   - `worker-index -c 2`
   - `worker_prefetch_multiplier=1`, so a worker never reserves tasks it can't start.
2. **Chaining by message, not a Celery chain:** each task enqueues the next stage after its commit. Every hop is a durable broker message, and a stage can be replayed on its own (P3).
3. **Delivery:** `acks_late=True`, `task_reject_on_worker_lost=True` on every task. The outbox relay publishes with `SELECT … FOR UPDATE SKIP LOCKED`, so several relays can run without double-publishing. Delivery is at-least-once, and duplicates are absorbed by idempotent stages (ADR 0002).
4. **Bounded task time:**
   - **Redis broker `visibility_timeout = 1800 s`.**
   - **Every stage task:** `soft_time_limit = 1200 s` (the stage fails with `TIME_LIMIT`) and `time_limit = 1500 s`.
   - **OCR caps** (settings):
     - `OCR_MAX_PAGES = 50` scanned pages, checked *before* any OCR runs → `OCR_PAGE_LIMIT`
     - `OCR_PAGE_TIMEOUT_S = 60` per page → `OCR_PAGE_TIMEOUT`
     - `OCR_BUDGET_S = 900` in total → `OCR_TIME_BUDGET`
   - Workers get `stop_grace_period: 120s`, so a deploy lets running tasks finish.

## Trade-off: the visibility timeout

With the Redis transport, a task that was delivered but not acked goes back on the queue only after `visibility_timeout`. That is the only way a task held by a **SIGKILLed or OOM-killed** worker gets redelivered.

- **Too long** (we first proposed 2 h): a killed worker stalls its document for hours, and P3's chaos test, which runs `docker kill` on the embed worker mid-run, would have to wait that long.
- **Too short:** a task that is *still running* beyond the timeout gets redelivered to another worker while the first copy continues. Idempotency keeps that correct, but the work is doubled.

We chose **30 min**, so every task must finish well inside it. That is what the time limits and OCR caps guarantee: OCR budget 900 s < soft limit 1200 s < hard limit 1500 s < visibility 1800 s.

The price: a very large scanned document fails with `OCR_PAGE_LIMIT` / `OCR_TIME_BUDGET` instead of being processed. The future option, if real corpora need it, is to split OCR into per-page subtasks on the `parse` queue, so no single task is long.

## Update (P3): what a kill actually costs, retries, and the chaos harness

Facts from the installed kombu/celery source:
- **A killed pool child** (OOM killer, `kill -9`): the worker's main process sees `WorkerLostError`, and `reject_on_worker_lost=True` **requeues the task at once**. The visibility timeout plays no part, and this is the most common real crash.
- **A killed container** (`docker kill`, a host crash): nothing acks or rejects. The message sits in Redis's `unacked` set until some worker's `restore_visible` finds it older than `visibility_timeout`. kombu runs that scan every 10 s but by default acts on only every 10th scan, so in practice **VT + up to about 100 s**. We set `unacked_restore_throttle=1`, so it becomes **VT + about 10 s**. The cost is one ZRANGE on the unacked index every 10 s per worker.
- **Retry countdowns** are ETA tasks that the worker holds *unacked*. A countdown longer than VT would be restored to another worker while the first still holds it, so **`retry_backoff_max` must be below VT** (300 < 1800).

**Retries.** Every stage task has `autoretry_for=(TransientError,)`, `retry_backoff=True`, `retry_backoff_max=300`, `retry_jitter=True`, `max_retries=5`.
- `run_stage` performs the retry itself for transient errors raised in a handler: it computes the countdown as full-jitter backoff, but never earlier than the upstream's `Retry-After`, capped at `retry_backoff_max`. Celery's autoretry can't see Retry-After, which is why `run_stage` does this; `autoretry_for` stays as the safety net for transient errors raised outside the handler.
- The OpenAI SDK's own retries are off in the workers (`max_retries=0`), so a 429 is visible to this policy instead of being retried underneath it.
- After `max_retries` the document is dead-lettered (`kind=exhausted`).
- A redelivery cap (`STAGE_ATTEMPT_LIMIT=8` per stage and version) stops a poison document that kills its worker on every delivery: the attempt count is committed *before* the handler runs, so a kill still counts.

**All timing is in settings, validated at startup:** `ocr_budget < soft < hard < visibility_timeout` and `retry_backoff_max < visibility_timeout`. The API, workers and relay refuse to start on an inconsistent set.

**The chaos harness can't wait 30 minutes per run**, so it runs the *same code with smaller numbers* via `docker-compose.chaos.yml`:
- VT 120 s, soft/hard limit 60/90 s, OCR budget 45 s, retry backoff max 30 s, embed batch 4
- `scripts/chaos_embed.py` reads the running worker's effective config and refuses to run if VT > 300 s or `APP_ENV=production`

**Trade-offs:**
- **What the chaos run proves:** the mechanism and the invariants (no duplicate chunks, every document ends INDEXED or FAILED, Redis = MySQL, no keys for non-active versions), not production latency. In production a killed *container* stalls the tasks it held for up to 30 min + 10 s; a killed *child* recovers at once. `--mode child` exercises that path.
- **Why not a short VT in production:** it would force every task under it. Long OCR jobs would either fail (a budget of about 1 min) or be redelivered while still running (correct thanks to idempotency, but double the CPU and API cost). Per-page OCR subtasks remain the way to shorten VT later.
- **Retry-After vs the cap:** a `Retry-After` longer than `retry_backoff_max` is capped, so the retry comes early. The alternative, an ETA longer than VT, would duplicate the task.

## Consequences

- Each queue can be scaled on its own (`docker compose up --scale worker-embed=3` raises the OpenAI concurrency cap on purpose).
- Five long-running services instead of one, which costs a little more memory (about 1 GB in total locally).
- Failures are stage-specific and visible per document (`document_stages`, shown in the UI as chips), which is the basis for P3's DLQ and replay.
