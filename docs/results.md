# Results

Measured results for PageCite: retrieval/answer eval and ingestion load (P5), chaos and DLQ (P3).
The two sections between `<!-- ... -->` markers are generated (`python -m eval.run --write-results`, `python -m load.run_load --write-results`); everything else is written by hand.

<!-- eval:start -->
## Eval: 4 retrieval configs on the RBI golden set (2026-11-25 17:51 UTC)

- Commit `f064092` + uncommitted changes; `python -m eval.run` with `configs/vector_only.yaml`, `configs/hybrid.yaml`, `configs/hybrid_rerank.yaml`, `configs/hybrid_rerank_decomp.yaml`.
- Corpus: 60 RBI notification PDFs, 395 pages (`eval/corpus.json`), tenant `eval-rbi`. Golden set: 47 rows (42 answerable, 5 unanswerable), reviewed against the PDFs (`eval/golden_review.md`).
- Models: answer `gpt-5.4-mini-2026-03-17`, decompose/rerank `gpt-5.4-nano-2026-03-17`, embeddings `text-embedding-3-small`, faithfulness judge `gpt-5.4-mini` (agreed with 8/8 hand-labelled judge-check cases, including two prompt-injection cases).
- One run per config: models are not deterministic, so differences of a row or two are within noise.

| Config | hit@5 | MRR | recall@5 (multi) | Faithfulness | Abstention acc. | False abstain | Unanswerable abstained | Avg tokens | Avg latency | Errors | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `vector_only` | 76.2% | 0.663 | 80.0% | 84.8% (46 sentences) | 91.5% | 9.5% | 100.0% | 2,326 | 1.8 s | 0 | $0.112 |
| `hybrid` | 85.7% | 0.740 | 80.0% | 93.0% (43 sentences) | 89.4% | 11.9% | 100.0% | 2,349 | 1.8 s | 0 | $0.112 |
| `hybrid_rerank` **(best)** | 90.5% | 0.800 | 90.0% | 83.7% (49 sentences) | 95.7% | 2.4% | 80.0% | 6,303 | 3.0 s | 0 | $0.155 |
| `hybrid_rerank_decomp` | 88.1% | 0.818 | 100.0% | 90.2% (51 sentences) | 95.7% | 2.4% | 80.0% | 6,247 | 4.4 s | 0 | $0.145 |

Metrics: hit@5 and MRR over the 42 answerable rows (a block hits if it is the expected document and its page range contains the expected page); faithfulness = judged answer sentences supported by the block(s) they cite, uncited sentences counted as unsupported; abstention accuracy over all rows. Tokens and latency per question cover retrieval + answer (decompose, query embedding, rerank, answer), not the judge. Cost per config includes its judge calls; total $0.52.

**Best config (rule fixed before the run: hit@5, then faithfulness, then tokens): `hybrid_rerank`** with hit@5 90.5%, MRR 0.800, faithfulness 83.7%.

### hit@5 by question kind, next to the lexical overlap of the questions

Overlap = share of the question's BM25 terms that also appear in the source quote (identifier and bank-type words excluded for those kinds). Low overlap means the text half of hybrid search gets no help from copied wording.

| Kind | Rows | Mean overlap | `vector_only` | `hybrid` | `hybrid_rerank` | `hybrid_rerank_decomp` |
|---|---|---|---|---|---|---|
| semantic | 22 | 0.21 | 95.5% | 90.9% | 90.9% | 90.9% |
| identifier | 8 | 0.16 | 37.5% | 62.5% | 75.0% | 75.0% |
| bank_type | 7 | 0.11 | 57.1% | 85.7% | 100.0% | 85.7% |
| multi | 5 | 0.19 | 80.0% | 100.0% | 100.0% | 100.0% |

<!-- eval:end -->

<!-- load:start -->
## Ingestion load: 200 documents in one batch (2026-11-25 18:23 UTC)

- Commit `f064092` + uncommitted changes; `python -m load.run_load`. Tenant `load-200-20261125-234530` (purged afterwards with `flask purge-tenant`).
- 200 uploads = the 60 corpus PDFs repeated (60 x 3 + 20); 1278 pages, 0 OCR'd, 3,765 chunks, 639,388 embedding tokens (`text-embedding-3-small`).
- Environment: Windows 10.0.26300 (Windows 11), 8 logical CPUs; Docker Desktop 29.6.1: 4 CPUs, 4.8 GiB; one container per stage queue (parse, chunk, embed, index), celery -c 2 each; parse mem_limit 1g.
- Upload phase (POST /batches + 200 x PUT + complete, 8 client threads): 18.2 s.

**Throughput: 25.2 documents/min** (200/200 INDEXED in 476 s, from the first `complete` to the last document indexed). Failed: 0; documents that needed a retry: 0.

| Stage | Docs | Run p50 | Run p95 | Run max | Queue wait p50 | Queue wait p95 | Peak queue depth | Retried docs |
|---|---|---|---|---|---|---|---|---|
| parse | 200 | 0.6 s | 12.1 s | 292.7 s | 57.5 s | 442.4 s | 173 | 0 |
| chunk | 200 | 0.0 s | 0.6 s | 9.2 s | 0.0 s | 0.9 s | 1 | 0 |
| embed | 200 | 0.6 s | 4.3 s | 36.0 s | 0.1 s | 2.3 s | 7 | 0 |
| index | 200 | 0.1 s | 1.2 s | 11.0 s | 0.0 s | 0.9 s | 4 | 0 |

Peak outbox backlog (completed, not yet enqueued by the relay): 20. Peak prefetched/ETA (kombu `unacked`): 8. `GET /api/health` through Caddy during the run: 137 probes, p95 2084 ms, max 4178 ms, 0 failed.

Run time = `finished_at - started_at` of the stage (version 1); queue wait = start of the stage minus the end of the previous one (for parse: minus the `complete`). Queue depth = messages waiting in the stage's broker queue, sampled every 0.5 s.

<!-- load:end -->

## Reading the numbers (P5)

**Retrieval.** Hybrid + rerank is the best config by the rule fixed before the run (hit@5 90.5% vs 76.2% for vector-only). The gain comes from the twin circulars, not from easy wording: question overlap with the source text is low for every kind (0.11–0.21), and on paraphrased semantic questions vector-only is already at 95.5% (hybrid: 90.9%). Where only an exact circular number or a bank type tells two near-identical documents apart, vector-only finds the right one for 37.5% / 57.1% of questions and hybrid + rerank for 75% / 100%. Decomposition adds latency (+1.4 s) and helps only the multi-part rows (recall@5 90% → 100%, MRR 0.800 → 0.818); on hit@5 it is one row behind.

**Faithfulness is the noisiest number.** Each config has only 43–51 judged sentences, so one sentence is about 2 points, and the order between configs (93.0 / 90.2 / 84.8 / 83.7%) should not be over-read. Two cases from the hand check of `hybrid_rerank`'s unsupported verdicts:
- a real failure: for the unanswerable payments-bank KYC question (`u01`), hybrid + rerank answered from the small-finance-bank twin instead of abstaining (both rerank configs abstained on 4 of 5 unanswerable rows; the other two configs on 5 of 5);
- a likely judge false negative: the penal-interest sentence of `m05` is in the cited chunk, but inside a PDF table that the text layer flattens out of order.
The run's details stored block numbers but not chunk ids, so not every verdict could be audited against its exact chunk; the runner now records chunk ids.

**Load.** Parse is the bottleneck: its queue peaked at 173 of 200 documents while chunk, embed and index never exceeded 7, and 0 documents needed a retry (no OpenAI rate limiting at embed concurrency 2). The parse tail is contention, not document size alone: the same 83-page circular parsed in 18 s as one copy and 293 s as another. `GET /api/health` through Caddy stayed up (0 of 137 failed) but slowed to p95 2.1 s with Docker Desktop's 4 CPUs saturated. The first load run failed after 67 completes with a 502: Caddy reused an upstream connection that gunicorn (keep-alive 2 s by default) was closing. Caddy now drops idle upstream connections after 30 s and gunicorn keeps them 75 s; a unit test keeps that order.

**Limitations.** One run per config (models are not deterministic). The judge uses the `gpt-5.4-mini` alias while the answers use the dated snapshot. 47 questions on one corpus. Load numbers are for a laptop (Docker Desktop: 4 CPUs, 4.8 GiB), with the 200 documents being the 60-file corpus repeated.

## Resume bullets (P5, measured)

- Built a hybrid retrieval pipeline (Redis 8.4 `FT.HYBRID` BM25 + vector with RRF, LLM rerank) that raised **hit@5 from 76.2% to 90.5%** over vector-only on a 47-question golden set of 60 real RBI circulars, and from 37.5% to 75% on questions where only an exact circular number separates near-identical documents.
- Measured answer faithfulness with an LLM judge validated 8/8 on hand-labelled and prompt-injection cases: **93.0% of cited answer sentences supported** with hybrid retrieval (83.7% with hybrid + rerank); answers cite their source page, and the rerank configs reach 95.7% abstention accuracy on answerable + unanswerable questions.
- Async ingestion (S3 presigned uploads → staged Celery workers → MySQL + Redis index) processed **200 PDFs (1,278 pages) at 25.2 documents/min** with 0 failures on a 4-CPU Docker Desktop, with per-stage p95 timings and queue depths that located the bottleneck (parsing); the load test also found and fixed a proxy keep-alive race that returned 502s.


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
