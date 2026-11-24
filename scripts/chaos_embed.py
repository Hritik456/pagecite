"""P3 chaos harness: kill the embed worker mid-run and check the pipeline's invariants.

Needs the chaos overlay (short visibility timeout, docs/adr/0003):

    docker compose -f docker-compose.yml -f docker-compose.chaos.yml up -d --build
    uv run python scripts/chaos_embed.py --runs 3                  # docker kill (default)
    uv run python scripts/chaos_embed.py --runs 1 --mode child     # kill -9 a pool child (OOM-like)

Each run:
  1. seeds a fresh tenant and builds 50 deterministic multi-page PDFs (expected chunk counts are
     computed locally with the same parser + chunker);
  2. uploads them as one batch through the API;
  3. waits until embed tasks are RUNNING (and some documents are INDEXED), then kills the embed
     worker: `docker kill` the container (its unacked task returns after the visibility timeout),
     or `kill -9` a pool child (requeued at once via reject_on_worker_lost); restarts it after 5 s;
  4. waits until every document is INDEXED or FAILED;
  5. checks the invariants:
     - every document INDEXED or FAILED;
     - no duplicate chunks: per-document count = expected, no repeated (doc, version, ord), every
       chunk embedded, exactly 4 processed_jobs per INDEXED document;
     - Redis count = MySQL count (active versions) and 0 keys for non-active versions;
     - the kill hit work in flight: >= 1 embed stage redelivered (attempts >= 2) and the documents
       that were RUNNING at kill time ended INDEXED.
Results go to docs/results.md. Exit code 0 only if every run passes. Uses real OpenAI embeddings
(about 170k tokens, well under $0.01 per run).
"""

import argparse
import hashlib
import io
import json
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from fpdf import FPDF
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke_ingest  # noqa: E402

from apps.api.auth import hash_key  # noqa: E402
from apps.api.config import Settings  # noqa: E402
from apps.api.search_index import count_tenant, index_config, make_redis  # noqa: E402
from apps.worker.chunking import chunk_pages  # noqa: E402
from apps.worker.parsing import parse_document  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "docs" / "results.md"
N_DOCS = 50
# Long enough that one embed task makes several API calls (about 15 chunks / EMBED_BATCH_SIZE=4), so a
# kill lands mid-task. With 3 pages a task took ~0.7 s and the kill arrived after it had finished.
PAGES = 12
MAX_CHAOS_VT_S = 300

WORDS = (
    "policy staff claim approval register officer notice period allowance section circular unit records "
    "audit review travel leave procurement budget committee deadline exemption department finance training"
).split()


def compose(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, check=check).stdout


def effective_worker_config() -> dict:
    """What the running embed worker actually uses (not what this machine's .env says)."""
    code = (
        "import json; from apps.api.config import Settings; s = Settings(); print(json.dumps({"
        "'app_env': s.app_env, 'vt': s.broker_visibility_timeout_s, 'soft': s.stage_soft_time_limit_s, "
        "'hard': s.stage_time_limit_s, 'backoff_max': s.retry_backoff_max_s, 'max_retries': s.max_retries, "
        "'embed_batch': s.embed_batch_size, 'fault_prefix': s.fault_embed_429_prefix}))"
    )
    return json.loads(compose("exec", "-T", "worker-embed", "python", "-c", code).strip().splitlines()[-1])


def preflight() -> dict:
    cfg = effective_worker_config()
    if cfg["app_env"].lower() == "production":
        sys.exit("refusing: the embed worker runs with APP_ENV=production")
    if cfg["vt"] > MAX_CHAOS_VT_S:
        sys.exit(
            f"refusing: visibility timeout is {cfg['vt']} s (> {MAX_CHAOS_VT_S}); start the chaos overlay:\n"
            "  docker compose -f docker-compose.yml -f docker-compose.chaos.yml up -d --build"
        )
    return cfg


def make_pdf(i: int, run: int) -> bytes:
    rng = random.Random(1000 * run + i)
    pdf = FPDF(format="A4")
    pdf.set_creation_date(datetime(2026, 1, 1, tzinfo=timezone.utc))
    pdf.set_auto_page_break(auto=True, margin=20)
    for page in range(PAGES):
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 13)
        pdf.multi_cell(0, 8, f"CHAOS DOCUMENT {run}-{i:02d} PART {page + 1}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        for _ in range(5):
            sentence = " ".join(rng.choice(WORDS) for _ in range(rng.randint(12, 20)))
            pdf.multi_cell(0, 5, (sentence.capitalize() + ". ") * 3, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def expected_chunks(data: bytes, settings: Settings) -> int:
    pages = parse_document(data, "application/pdf", lambda image, timeout: "")
    return len(chunk_pages(pages, settings.chunk_tokens, settings.chunk_overlap, settings.tokenizer))


def upload(api: str, key: str, files: list[tuple[str, bytes]]) -> tuple[int, dict[str, int]]:
    headers = {"X-Tenant-Key": key}
    spec = [{"name": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest(), "mime": "application/pdf"}
            for n, d in files]
    body = requests.post(f"{api}/batches", json={"files": spec}, headers=headers, timeout=30)
    body.raise_for_status()
    body = body.json()
    ids = {}
    for up, (name, data) in zip(body["uploads"], files):
        requests.put(up["presignedUrl"], data=data, headers=up["headers"], timeout=60).raise_for_status()
        requests.post(f"{api}/documents/{up['documentId']}/complete", headers=headers, timeout=30).raise_for_status()
        ids[name] = up["documentId"]
    return body["batchId"], ids


def q(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).all()


def kill_embed_worker(mode: str, container: str) -> None:
    if mode == "container":
        subprocess.run(["docker", "kill", container], check=True, capture_output=True)
    else:  # SIGKILL every pool child of the celery main process (PID 1 in the container)
        compose("exec", "-T", "worker-embed", "sh", "-c", "kill -9 $(cat /proc/1/task/1/children)")


def run_once(run: int, args, settings: Settings, engine, r, cfg_index) -> dict:
    print(f"\n=== run {run}: seeding a tenant and building {N_DOCS} PDFs")
    key = smoke_ingest.seed_tenant()
    tenant_id = q(engine, "SELECT id FROM tenants WHERE api_key_hash = :h", h=hash_key(key))[0][0]
    files = [(f"chaos-{run}-{i:02d}.pdf", make_pdf(i, run)) for i in range(N_DOCS)]
    expected = {name: expected_chunks(data, settings) for name, data in files}
    batch_id, ids = upload(args.api, key, files)
    by_id = {doc_id: name for name, doc_id in ids.items()}
    print(f"uploaded batch {batch_id}: {N_DOCS} docs, {sum(expected.values())} expected chunks")

    # 3. wait for an embed task that has JUST started (it runs ~3 s), then kill at once
    container = compose("ps", "-q", "worker-embed").strip()  # resolved up front: the kill is one call
    deadline = time.monotonic() + 300
    while True:
        running = [row[0] for row in q(engine,
            "SELECT s.document_id FROM document_stages s JOIN documents d ON d.id = s.document_id "
            "WHERE d.batch_id = :b AND s.stage = 'embed' AND s.status = 'RUNNING' "
            "AND s.started_at > NOW(6) - INTERVAL 0.5 SECOND", b=batch_id)]
        indexed = q(engine, "SELECT COUNT(*) FROM documents WHERE batch_id = :b AND status = 'INDEXED'", b=batch_id)[0][0]
        if running and indexed >= 3:
            break
        if time.monotonic() > deadline:
            raise SystemExit("embed never started")
        time.sleep(0.1)
    killed_at = time.monotonic()
    kill_embed_worker(args.mode, container)
    print(f"killed the embed worker ({args.mode}) with {len(running)} embed task(s) RUNNING: docs {running}; "
          f"{indexed} INDEXED so far")
    time.sleep(5)
    if args.mode == "container":
        compose("start", "worker-embed")
    restarted_at = time.monotonic()

    # 4. wait until every document is terminal
    limit = restarted_at + args.vt + 300  # the killed task returns after the visibility timeout
    last = ""
    while True:
        counts = dict(q(engine, "SELECT status, COUNT(*) FROM documents WHERE batch_id = :b GROUP BY status", b=batch_id))
        summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        if summary != last:
            print(f"  t+{time.monotonic() - killed_at:5.0f}s  {summary}")
            last = summary
        if set(counts) <= {"INDEXED", "FAILED"}:
            break
        if time.monotonic() > limit:
            break
        time.sleep(2)
    settled_at = time.monotonic()

    # 5. invariants
    checks = smoke_ingest.Checks()
    counts = dict(q(engine, "SELECT status, COUNT(*) FROM documents WHERE batch_id = :b GROUP BY status", b=batch_id))
    n_indexed, n_failed = counts.get("INDEXED", 0), counts.get("FAILED", 0)
    checks.add("every document INDEXED or FAILED", n_indexed + n_failed == N_DOCS, json.dumps(counts))

    actual = dict(q(engine,
        "SELECT c.document_id, COUNT(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE d.batch_id = :b AND c.version = d.active_version GROUP BY c.document_id", b=batch_id))
    wrong = {by_id[d]: (n, expected[by_id[d]]) for d, n in actual.items() if n != expected[by_id[d]]}
    checks.add("chunk count per document = expected (no duplicates, none missing)",
               not wrong and len(actual) == n_indexed, f"{len(wrong)} mismatched" if wrong else f"{sum(actual.values())} chunks")
    dup = q(engine,
        "SELECT COUNT(*) FROM (SELECT c.document_id FROM chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE d.batch_id = :b GROUP BY c.document_id, c.version, c.ord HAVING COUNT(*) > 1) x", b=batch_id)[0][0]
    checks.add("no repeated (document, version, ord)", dup == 0, f"{dup} groups")
    unembedded = q(engine,
        "SELECT COUNT(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE d.batch_id = :b AND d.status = 'INDEXED' AND (NOT c.embedding_done OR c.embedding IS NULL)", b=batch_id)[0][0]
    checks.add("every chunk of an INDEXED document embedded", unembedded == 0, f"{unembedded} not embedded")
    jobs = q(engine,
        "SELECT COUNT(*) FROM processed_jobs p JOIN documents d "
        "ON p.job_key LIKE CONCAT('doc', CHAR(58), d.id, CHAR(58), '%', CHAR(58), 'v', d.processing_version) "
        "WHERE d.batch_id = :b AND d.status = 'INDEXED'", b=batch_id)[0][0]
    checks.add("exactly 4 processed_jobs per INDEXED document", jobs == 4 * n_indexed, f"{jobs} for {n_indexed} docs")

    mysql_active = q(engine,
        "SELECT COUNT(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE c.tenant_id = :t AND c.version = d.active_version", t=tenant_id)[0][0]
    redis_count = count_tenant(r, cfg_index, tenant_id)
    checks.add("Redis count = MySQL count (active versions)", redis_count == mysql_active,
               f"redis={redis_count} mysql={mysql_active}")
    key_ids = [int(k.decode().rsplit(":", 1)[1]) for k in r.scan_iter(match=f"{cfg_index.prefix}{tenant_id}:*", count=1000)]
    inactive = 0
    for start in range(0, len(key_ids), 1000):
        ids_sql = ",".join(str(i) for i in key_ids[start:start + 1000])
        active = q(engine,
            f"SELECT COUNT(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
            f"WHERE c.id IN ({ids_sql}) AND c.version = d.active_version")[0][0]
        inactive += len(key_ids[start:start + 1000]) - active
    checks.add("0 Redis keys for non-active versions", inactive == 0, f"{inactive} stale/orphan keys")

    redelivered = q(engine,
        "SELECT COUNT(*), COALESCE(MAX(s.attempts), 0) FROM document_stages s JOIN documents d ON d.id = s.document_id "
        "WHERE d.batch_id = :b AND s.stage = 'embed' AND s.attempts >= 2", b=batch_id)[0]
    checks.add("the kill hit work in flight (>= 1 embed task redelivered)", redelivered[0] >= 1,
               f"{redelivered[0]} redelivered, max attempts {redelivered[1]}")
    killed_states = dict(q(engine, f"SELECT id, status FROM documents WHERE id IN ({','.join(map(str, running))})"))
    checks.add("documents RUNNING at kill time ended INDEXED", set(killed_states.values()) == {"INDEXED"},
               json.dumps(killed_states))

    passed = checks.ok
    print(f"run {run}: {'PASS' if passed else 'FAIL'} ({sum(ok for _, ok, _ in checks.results)}/{len(checks.results)})")
    return {
        "run": run, "mode": args.mode, "batch": batch_id, "docs": N_DOCS, "indexed": n_indexed, "failed": n_failed,
        "chunks": sum(actual.values()), "running_at_kill": len(running), "redelivered": redelivered[0],
        "max_attempts": redelivered[1], "recovery_s": round(settled_at - restarted_at), "duplicates": dup,
        "redis_eq_mysql": redis_count == mysql_active, "inactive_keys": inactive, "pass": passed,
        "failed_checks": [name for name, ok, _ in checks.results if not ok],
    }


def write_results(rows: list[dict], cfg: dict) -> None:
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if not RESULTS.exists():
        RESULTS.write_text("# Results\n\nMeasured results for PageCite (chaos tests, DLQ demo; P5 adds eval and load).\n",
                           encoding="utf-8", newline="\n")
    lines = [
        "",
        f"## Chaos: embed worker killed mid-run ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
        "",
        f"- Commit `{sha}`{' (+ uncommitted changes)' if dirty else ''}; `scripts/chaos_embed.py --runs {len(rows)} "
        f"--mode {rows[0]['mode']}`, {N_DOCS} PDFs per run, real OpenAI embeddings.",
        f"- Worker config: APP_ENV={cfg['app_env']}, visibility timeout {cfg['vt']} s, soft/hard time limit "
        f"{cfg['soft']}/{cfg['hard']} s, retry backoff max {cfg['backoff_max']} s, embed batch {cfg['embed_batch']}, "
        f"restore scan every 10 s (unacked_restore_throttle=1).",
        "",
        "| Run | Kill | Docs | INDEXED | FAILED | Chunks | Embed tasks running at kill | Redelivered | Max attempts "
        "| Recovery after restart | Duplicate chunks | Redis = MySQL | Non-active keys | Result |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['run']} | {row['mode']} | {row['docs']} | {row['indexed']} | {row['failed']} | {row['chunks']} "
            f"| {row['running_at_kill']} | {row['redelivered']} | {row['max_attempts']} | {row['recovery_s']} s "
            f"| {row['duplicates']} | {'yes' if row['redis_eq_mysql'] else 'NO'} | {row['inactive_keys']} "
            f"| {'PASS' if row['pass'] else 'FAIL: ' + ', '.join(row['failed_checks'])} |"
        )
    lines.append(f"\n**{sum(r['pass'] for r in rows)}/{len(rows)} runs passed.**\n")
    with open(RESULTS, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    print(f"\nresults appended to {RESULTS.relative_to(ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000/api")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--mode", choices=["container", "child"], default="container")
    args = parser.parse_args()

    cfg = preflight()
    args.vt = cfg["vt"]
    print(f"embed worker config: {cfg}")
    settings = Settings()  # host-side URLs (.env); chunking settings match the workers
    engine = create_engine(settings.database_url)
    r = make_redis(settings.redis_url)
    rows = [run_once(n, args, settings, engine, r, index_config(settings)) for n in range(1, args.runs + 1)]
    write_results(rows, cfg)
    ok = all(row["pass"] for row in rows)
    print(f"\n{'PASS' if ok else 'FAIL'}: {sum(row['pass'] for row in rows)}/{len(rows)} runs")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
