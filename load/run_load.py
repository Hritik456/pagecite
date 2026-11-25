"""P5 ingestion load test: 200 documents in one batch through the real pipeline (local stack only).

    uv run python -m load.run_load --estimate           # embedding cost from the eval tenant's chunk tokens
    uv run python -m load.run_load --write-results      # run, then update docs/results.md
    # afterwards: uv run flask --app apps.api purge-tenant --tenant <id> --yes   (host .env: localhost services)

1. A fresh tenant `load-200-<timestamp>` (200-file batch limit). Never the eval tenant.
2. 200 uploads = the 60 pinned corpus PDFs repeated (60 x 3 + 20), named load-001-<file> ... There is
   no sha256 dedupe, so every copy is parsed, chunked, embedded and indexed in full.
3. One POST /batches, then PUT + complete with 8 threads, so the batch lands within seconds.
4. A sampler every 0.5 s, all read-only, each on a short connection:
   - queue depth per stage: LLEN of the queue and its priority sub-queues on the broker (Redis DB 1)
   - prefetched/ETA tasks: HLEN of kombu's `unacked` hash
   - outbox events not yet published, document status counts (MySQL)
   - GET /api/health through Caddy (latency, failures): an API stall under load is measured
   Progress comes from MySQL, not the API, so a slow API cannot stop the run.
5. Results from MySQL timestamps (one clock): docs/min = INDEXED / (last index finished - first
   outbox event, i.e. the first `complete`); p50/p95/max run time per stage and queue wait before it;
   documents that needed retries; failures. Deadline 45 min, then stuck documents are listed.

This shares the workers with every tenant, so it is for the local stack, never the deployed one.
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import redis
import requests
from sqlalchemy import create_engine, text

from apps.api.config import Settings

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import smoke_ingest  # noqa: E402

from eval.build_corpus import PDF_DIR, load_manifest  # noqa: E402
from eval.cost import price_for  # noqa: E402
from eval.ingest_corpus import TENANT as EVAL_TENANT, tenant_id_by_name  # noqa: E402

OUT = ROOT / "load" / "out"
RESULTS = ROOT / "docs" / "results.md"
N_DOCS = 200
QUEUES = ("parse", "chunk", "embed", "index")
PRIORITY_SEP, PRIORITY_STEPS = "\x06\x16", (3, 6, 9)  # kombu redis transport (5.7)
TERMINAL = ("INDEXED", "FAILED", "CANCELLED")
LOAD_START, LOAD_END = "<!-- load:start -->", "<!-- load:end -->"


def plan_files(n: int = N_DOCS) -> list[tuple[str, str]]:
    """(upload name, corpus file) for n uploads, cycling through the pinned corpus."""
    files = [f["file"] for f in load_manifest()["files"]]
    return [(f"load-{i + 1:03d}-{files[i % len(files)]}", files[i % len(files)]) for i in range(n)]


def queue_depths(broker: redis.Redis) -> dict[str, int]:
    with broker.pipeline() as pipe:
        for q in QUEUES:
            pipe.llen(q)
            for step in PRIORITY_STEPS:
                pipe.llen(f"{q}{PRIORITY_SEP}{step}")
        pipe.hlen("unacked")
        sizes = pipe.execute()
    per = 1 + len(PRIORITY_STEPS)
    out = {q: sum(sizes[i * per:(i + 1) * per]) for i, q in enumerate(QUEUES)}
    out["unacked"] = sizes[-1]
    return out


class Sampler(threading.Thread):
    def __init__(self, engine, broker: redis.Redis, health_url: str, tenant_id: int, interval: float = 0.5):
        super().__init__(daemon=True)
        self.engine, self.broker, self.health_url, self.tenant_id = engine, broker, health_url, tenant_id
        self.interval = interval
        self.samples: list[dict] = []
        self.stop = threading.Event()
        self._last_health = 0.0

    def run(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            s: dict = {"t": time.time()}
            try:
                s["queues"] = queue_depths(self.broker)
            except redis.RedisError as exc:
                s["queues_error"] = type(exc).__name__
            try:
                with self.engine.connect() as conn:
                    s["status"] = dict(conn.execute(text(
                        "SELECT status, COUNT(*) FROM documents WHERE tenant_id = :t GROUP BY status"),
                        {"t": self.tenant_id}).all())
                    s["outbox_pending"] = conn.execute(text(
                        "SELECT COUNT(*) FROM outbox_events o JOIN documents d ON d.id = o.aggregate_id "
                        "WHERE d.tenant_id = :t AND o.published_at IS NULL"), {"t": self.tenant_id}).scalar()
            except Exception as exc:  # noqa: BLE001 - a sample is best-effort; record and continue
                s["db_error"] = type(exc).__name__
            if time.monotonic() - self._last_health >= 2.0:
                self._last_health = time.monotonic()
                t0 = time.perf_counter()
                try:
                    ok = requests.get(self.health_url, timeout=10).status_code == 200
                except requests.RequestException:
                    ok = False
                s["health_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                s["health_ok"] = ok
            self.samples.append(s)
            self.stop.wait(max(0.0, self.interval - (time.monotonic() - started)))

    def done(self, n: int) -> bool:
        last = next((s for s in reversed(self.samples) if "status" in s), None)
        return last is not None and sum(last["status"].get(k, 0) for k in TERMINAL) >= n


def upload_all(api: str, key: str, uploads: list[tuple[str, str]], threads: int = 8) -> tuple[int, float]:
    data = {f: (PDF_DIR / f).read_bytes() for f in {f for _, f in uploads}}
    spec = [{"name": name, "size": len(data[f]), "sha256": hashlib.sha256(data[f]).hexdigest(),
             "mime": "application/pdf"} for name, f in uploads]
    headers = {"X-Tenant-Key": key}
    started = time.perf_counter()
    resp = requests.post(f"{api}/batches", json={"files": spec}, headers=headers, timeout=60)
    resp.raise_for_status()
    body = resp.json()

    def one(args):
        up, (_, f) = args
        requests.put(up["presignedUrl"], data=data[f], headers=up["headers"], timeout=120).raise_for_status()
        requests.post(f"{api}/documents/{up['documentId']}/complete", headers=headers, timeout=60).raise_for_status()

    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(one, zip(body["uploads"], uploads)))
    return body["batchId"], time.perf_counter() - started


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, round(p / 100 * len(v) + 0.5) - 1))]  # nearest-rank


def stage_stats(engine, tenant_id: int) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT s.document_id, s.stage, s.status, s.attempts, s.started_at, s.finished_at "
            "FROM document_stages s JOIN documents d ON d.id = s.document_id "
            "WHERE d.tenant_id = :t AND s.version = 1"), {"t": tenant_id}).all()
        first_complete = conn.execute(text(
            "SELECT MIN(o.created_at) FROM outbox_events o JOIN documents d ON d.id = o.aggregate_id "
            "WHERE d.tenant_id = :t"), {"t": tenant_id}).scalar()
        completes = dict(conn.execute(text(
            "SELECT o.aggregate_id, MIN(o.created_at) FROM outbox_events o JOIN documents d ON d.id = o.aggregate_id "
            "WHERE d.tenant_id = :t GROUP BY o.aggregate_id"), {"t": tenant_id}).all())
        statuses = dict(conn.execute(text(
            "SELECT status, COUNT(*) FROM documents WHERE tenant_id = :t GROUP BY status"), {"t": tenant_id}).all())
        chunks = conn.execute(text("SELECT COUNT(*), COALESCE(SUM(token_count), 0) FROM chunks WHERE tenant_id = :t"),
                              {"t": tenant_id}).one()
        pages = conn.execute(text(
            "SELECT COUNT(*), COALESCE(SUM(p.ocr), 0) FROM document_pages p JOIN documents d ON d.id = p.document_id "
            "WHERE d.tenant_id = :t"), {"t": tenant_id}).one()

    by_doc: dict[int, dict[str, tuple]] = {}
    for doc_id, stage, status, attempts, started, finished in rows:
        by_doc.setdefault(doc_id, {})[stage] = (status, attempts, started, finished)
    out: dict = {"stages": {}}
    prev = {"parse": None, "chunk": "parse", "embed": "chunk", "index": "embed"}
    for stage in QUEUES:
        run, wait, retried = [], [], 0
        for doc_id, st in by_doc.items():
            if stage not in st:
                continue
            status, attempts, started, finished = st[stage]
            retried += attempts > 1
            if started and finished:
                run.append((finished - started).total_seconds())
            before = completes.get(doc_id) if prev[stage] is None else (st.get(prev[stage]) or (None,) * 4)[3]
            if started and before:
                wait.append(max(0.0, (started - before).total_seconds()))
        out["stages"][stage] = {
            "n": len(run), "retried_docs": retried,
            "run_p50_s": pct(run, 50), "run_p95_s": pct(run, 95), "run_max_s": max(run) if run else None,
            "wait_p50_s": pct(wait, 50), "wait_p95_s": pct(wait, 95),
        }
    index_done = [st["index"][3] for st in by_doc.values() if "index" in st and st["index"][3]]
    last = max(index_done) if index_done else None
    indexed = statuses.get("INDEXED", 0)
    elapsed = (last - first_complete).total_seconds() if last and first_complete else None
    out.update(
        statuses=statuses, indexed=indexed, chunks=chunks[0], chunk_tokens=int(chunks[1]), pages=pages[0],
        ocr_pages=int(pages[1]), first_complete=str(first_complete), last_indexed=str(last), elapsed_s=elapsed,
        docs_per_min=(indexed / (elapsed / 60)) if elapsed else None,
        retried_docs=len({d for d, st in by_doc.items() if any(v[1] > 1 for v in st.values())}),
    )
    return out


def environment() -> dict:
    info = subprocess.run(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}} {{.ServerVersion}}"],
                          capture_output=True, text=True).stdout.split()
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                                capture_output=True, text=True).stdout.strip())
    return {
        "host": f"{platform.system()} {platform.version()}, {os.cpu_count()} logical CPUs",
        "docker": f"Docker Desktop {info[2]}: {info[0]} CPUs, {int(info[1]) / 2**30:.1f} GiB" if len(info) == 3 else "?",
        "workers": "one container per stage queue (parse, chunk, embed, index), celery -c 2 each; parse mem_limit 1g",
        "git_sha": sha, "dirty": dirty,
    }


def estimate(settings: Settings, engine) -> float:
    """Embedding tokens of the same 60 files, already measured in the eval tenant, x copies."""
    tenant = tenant_id_by_name(engine, EVAL_TENANT)
    if tenant is None:
        raise SystemExit("needs the eval tenant (python -m eval.ingest_corpus) to measure tokens per file")
    with engine.connect() as conn:
        per_file = dict(conn.execute(text(
            "SELECT d.filename, SUM(c.token_count) FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE d.tenant_id = :t AND c.version = d.active_version GROUP BY d.filename"), {"t": tenant}).all())
    tokens = sum(int(per_file.get(f, 0)) for _, f in plan_files())
    usd = tokens * price_for(settings.openai_embed_model).input / 1_000_000
    print(f"{N_DOCS} uploads: about {tokens:,} embedding tokens -> ${usd:.4f} ({settings.openai_embed_model})")
    return usd


def markdown(result: dict) -> str:
    st, env, peaks = result["stats"], result["env"], result["peaks"]
    fmt = lambda v: "n/a" if v is None else f"{v:.1f} s"  # noqa: E731
    lines = [
        LOAD_START,
        f"## Ingestion load: {N_DOCS} documents in one batch ({result['at']})",
        "",
        f"- Commit `{env['git_sha'][:7]}`{' + uncommitted changes' if env['dirty'] else ''}; `python -m load.run_load`. "
        f"Tenant `{result['tenant']}` (purged afterwards with `flask purge-tenant`).",
        f"- {N_DOCS} uploads = the 60 corpus PDFs repeated (60 x 3 + 20); {st['pages']} pages, {st['ocr_pages']} OCR'd, "
        f"{st['chunks']:,} chunks, {st['chunk_tokens']:,} embedding tokens (`text-embedding-3-small`).",
        f"- Environment: {env['host']}; {env['docker']}; {env['workers']}.",
        f"- Upload phase (POST /batches + 200 x PUT + complete, 8 client threads): {result['upload_s']:.1f} s.",
        "",
        f"**Throughput: {st['docs_per_min']:.1f} documents/min** ({st['indexed']}/{N_DOCS} INDEXED in "
        f"{st['elapsed_s']:.0f} s, from the first `complete` to the last document indexed). "
        f"Failed: {st['statuses'].get('FAILED', 0)}; documents that needed a retry: {st['retried_docs']}.",
        "",
        "| Stage | Docs | Run p50 | Run p95 | Run max | Queue wait p50 | Queue wait p95 | Peak queue depth | Retried docs |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for stage in QUEUES:
        s = st["stages"][stage]
        lines.append(f"| {stage} | {s['n']} | {fmt(s['run_p50_s'])} | {fmt(s['run_p95_s'])} | {fmt(s['run_max_s'])} | "
                     f"{fmt(s['wait_p50_s'])} | {fmt(s['wait_p95_s'])} | {peaks[stage]} | {s['retried_docs']} |")
    h = result["health"]
    lines += [
        "",
        f"Peak outbox backlog (completed, not yet enqueued by the relay): {peaks['outbox']}. Peak prefetched/ETA "
        f"(kombu `unacked`): {peaks['unacked']}. `GET /api/health` through Caddy during the run: {h['n']} probes, "
        f"p95 {h['p95_ms']:.0f} ms, max {h['max_ms']:.0f} ms, {h['failed']} failed.",
        "",
        "Run time = `finished_at - started_at` of the stage (version 1); queue wait = start of the stage minus the end "
        "of the previous one (for parse: minus the `complete`). Queue depth = messages waiting in the stage's broker "
        "queue, sampled every 0.5 s.",
        "",
        LOAD_END,
    ]
    return "\n".join(lines)


def write_results(md: str) -> None:
    text_ = RESULTS.read_text(encoding="utf-8")
    if LOAD_START in text_ and LOAD_END in text_:
        head, rest = text_.split(LOAD_START, 1)
        _, tail = rest.split(LOAD_END, 1)
        text_ = head + md + tail
    else:
        anchor = "<!-- eval:end -->"
        if anchor in text_:
            head, tail = text_.split(anchor, 1)
            text_ = head + anchor + "\n\n" + md + tail
        else:
            text_ = text_.rstrip() + "\n\n" + md + "\n"
    RESULTS.write_text(text_, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000/api")
    parser.add_argument("--estimate", action="store_true")
    parser.add_argument("--deadline", type=int, default=45 * 60)
    parser.add_argument("--write-results", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    if args.estimate:
        estimate(settings, engine)
        return 0

    missing = [f for _, f in plan_files() if not (PDF_DIR / f).is_file()]
    if missing:
        raise SystemExit(f"corpus files missing (e.g. {missing[0]}); run corpus/download-rbi-pdfs.ps1")
    name = f"load-200-{time.strftime('%Y%m%d-%H%M%S')}"
    key = smoke_ingest.seed_tenant(name, max_batch_files=N_DOCS)
    tenant_id = tenant_id_by_name(engine, name)
    broker = redis.Redis.from_url(settings.celery_broker_url, protocol=2)
    sampler = Sampler(engine, broker, args.api.rstrip("/") + "/health", tenant_id)
    sampler.start()

    print(f"tenant {name} (id {tenant_id}); uploading {N_DOCS} documents ...", flush=True)
    batch_id, upload_s = upload_all(args.api, key, plan_files())
    print(f"batch {batch_id}: uploads + completes done in {upload_s:.1f} s; waiting for the pipeline ...", flush=True)
    deadline = time.monotonic() + args.deadline
    last = ""
    while not sampler.done(N_DOCS) and time.monotonic() < deadline:
        time.sleep(5)
        s = next((s for s in reversed(sampler.samples) if "status" in s), {})
        line = " ".join(f"{k}={v}" for k, v in sorted(s.get("status", {}).items()))
        q = s.get("queues", {})
        if line != last:
            print(f"  {time.strftime('%H:%M:%S')} {line} | queues {q}", flush=True)
            last = line
    sampler.stop.set()
    sampler.join(timeout=15)
    timed_out = not sampler.done(N_DOCS)

    stats = stage_stats(engine, tenant_id)
    samples = sampler.samples
    peaks = {q: max((s["queues"][q] for s in samples if "queues" in s), default=0) for q in (*QUEUES, "unacked")}
    peaks["outbox"] = max((s["outbox_pending"] for s in samples if "outbox_pending" in s), default=0)
    health = [s["health_ms"] for s in samples if "health_ms" in s]
    result = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "tenant": name, "tenant_id": tenant_id,
        "batch_id": batch_id, "upload_s": upload_s, "timed_out": timed_out, "stats": stats, "peaks": peaks,
        "health": {"n": len(health), "p95_ms": pct(health, 95) or 0, "max_ms": max(health, default=0),
                   "failed": sum(1 for s in samples if s.get("health_ok") is False)},
        "env": environment(),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{time.strftime('%Y%m%dT%H%M%S')}-load.json"
    out.write_text(json.dumps({**result, "samples": samples}, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "env"}, indent=1, default=str))
    print(f"details: {out.relative_to(ROOT)}")
    if timed_out:
        with engine.connect() as conn:
            stuck = conn.execute(text("SELECT id, status FROM documents WHERE tenant_id = :t AND status NOT IN "
                                      "('INDEXED','FAILED','CANCELLED') LIMIT 20"), {"t": tenant_id}).all()
        print(f"DEADLINE reached; stuck documents: {stuck}")
        return 1
    if args.write_results:
        write_results(markdown(result))
        print(f"updated {RESULTS.relative_to(ROOT)}")
    print(f"cleanup: uv run flask --app apps.api purge-tenant --tenant {tenant_id} --yes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
