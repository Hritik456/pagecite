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

## Consequences

- Each queue can be scaled on its own (`docker compose up --scale worker-embed=3` raises the OpenAI concurrency cap on purpose).
- Five long-running services instead of one, which costs a little more memory (about 1 GB in total locally).
- Failures are stage-specific and visible per document (`document_stages`, shown in the UI as chips), which is the basis for P3's DLQ and replay.
