"""P3 DLQ demo on the real stack (Done-when #2): a mocked failure exhausts its retries, lands in the
DLQ, and replaying it ends INDEXED as version 2.

Needs the chaos overlay, which sets FAULT_EMBED_429_PREFIX=fail- (refused in production):

    docker compose -f docker-compose.yml -f docker-compose.chaos.yml up -d --build
    uv run python scripts/smoke_dlq.py

Uploads `fail-dlq-demo.pdf` (a copy of a sample). Its embed stage gets HTTP 429 (Retry-After: 1)
for version 1, so the real Celery retry path runs: RETRY_WAIT with backoff + jitter, 1 try + 5
retries, then the DLQ. The script then replays it through the API (version 2, not faulted) and
checks INDEXED, active_version = 2, only v2 keys in Redis, and that /ask cites it.
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chaos_embed  # noqa: E402
import smoke_ask  # noqa: E402
import smoke_ingest  # noqa: E402

from apps.api.auth import hash_key  # noqa: E402
from apps.api.config import Settings  # noqa: E402
from apps.api.search_index import index_config, make_redis  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
API = "http://localhost:8000/api"
FILE = "fail-dlq-demo.pdf"


def main() -> int:
    cfg = chaos_embed.preflight()
    if not cfg["fault_prefix"] or not FILE.startswith(cfg["fault_prefix"]):
        sys.exit("refusing: the embed worker has no FAULT_EMBED_429_PREFIX matching 'fail-'; use the chaos overlay")
    settings = Settings()
    engine = create_engine(settings.database_url)
    r, idx = make_redis(settings.redis_url), index_config(settings)
    checks = smoke_ingest.Checks()
    timeline: list[str] = []

    def q(sql, **p):
        with engine.connect() as conn:
            return conn.execute(text(sql), p).all()

    def note(msg: str) -> None:
        line = f"t+{time.monotonic() - started:5.1f}s  {msg}"
        timeline.append(line)
        print("  " + line)

    key = smoke_ingest.seed_tenant()
    headers = {"X-Tenant-Key": key}
    tenant_id = q("SELECT id FROM tenants WHERE api_key_hash = :h", h=hash_key(key))[0][0]
    data = (ROOT / "samples" / "01-leave-policy.pdf").read_bytes()
    started = time.monotonic()
    _, ids = chaos_embed.upload(API, key, [(FILE, data)])
    doc = ids[FILE]
    print(f"uploaded {FILE} as document {doc}; watching version 1 ...")

    last = None
    while time.monotonic() - started < 300:
        status = q("SELECT status FROM documents WHERE id = :d", d=doc)[0][0]
        stage = q("SELECT attempts, status, last_error FROM document_stages WHERE document_id = :d AND stage = 'embed' "
                  "AND version = 1", d=doc)
        state = (status, stage[0][0], stage[0][1]) if stage else (status, None, None)
        if state != last:
            note(f"document {status}; embed attempts={state[1]} stage={state[2]}"
                 + (f" ({stage[0][2][:40]})" if stage and stage[0][2] else ""))
            last = state
        if status in ("FAILED", "INDEXED"):
            break
        time.sleep(0.5)

    dlq = requests.get(f"{API}/dead-letters", headers=headers, timeout=30).json()["deadLetters"]
    mine = [d for d in dlq if d["documentId"] == doc]
    checks.add("v1 FAILED after its retries", last and last[0] == "FAILED", str(last))
    checks.add("1 try + 5 retries (6 attempts)", last and last[1] == 6, f"attempts={last and last[1]}")
    checks.add("in the DLQ: kind=exhausted, RATE_LIMITED",
               len(mine) == 1 and (mine[0]["kind"], mine[0]["errorCode"]) == ("exhausted", "RATE_LIMITED"),
               json.dumps(mine[0] if mine else None))

    resp = requests.post(f"{API}/documents/{doc}/replay", headers=headers, timeout=30)
    note(f"POST /documents/{doc}/replay -> {resp.status_code} version={resp.json()['document']['processingVersion']}")
    while time.monotonic() - started < 600:
        status, version, active = q("SELECT status, processing_version, active_version FROM documents WHERE id = :d", d=doc)[0]
        if status in ("INDEXED", "FAILED"):
            break
        time.sleep(0.5)
    note(f"document {status}; processing_version={version} active_version={active}")

    checks.add("replay ends INDEXED as version 2", (status, version, active) == ("INDEXED", 2, 2),
               f"{status} v{version} active={active}")
    key_ids = [int(k.decode().rsplit(":", 1)[1]) for k in r.scan_iter(match=f"{idx.prefix}{tenant_id}:*", count=1000)]
    versions = {row[0] for row in q(f"SELECT DISTINCT version FROM chunks WHERE id IN ({','.join(map(str, key_ids)) or 0})")}
    checks.add("Redis holds only v2 keys", versions == {2}, f"versions={sorted(versions)} keys={len(key_ids)}")
    checks.add("DLQ row marked replayed",
               requests.get(f"{API}/dead-letters", headers=headers, timeout=30).json()["deadLetters"] == [])
    answer = smoke_ask.ask(API, key, "What is the cobalt heron allowance?")
    cited = {c["document_id"] for c in answer["citations"]}
    checks.add("/ask cites the replayed document", doc in cited, f"cited={sorted(cited)} answer={answer['answer'][:80]!r}")

    ok = checks.ok
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    with open(ROOT / "docs" / "results.md", "a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join([
            "",
            f"## DLQ + replay demo ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
            "",
            f"- Commit `{sha}`; `scripts/smoke_dlq.py` on the chaos overlay (FAULT_EMBED_429_PREFIX=fail-, retry "
            f"backoff max {cfg['backoff_max']} s, max retries {cfg['max_retries']}).",
            f"- `{FILE}` (document {doc}): version 1 gets HTTP 429 with Retry-After: 1 on every embed call.",
            "",
            "```",
            *timeline,
            "```",
            "",
            *[f"- [{'x' if ok_ else ' '}] {name}{' (' + detail + ')' if detail else ''}" for name, ok_, detail in checks.results],
            "",
            f"**{'PASS' if ok else 'FAIL'}: {sum(o for _, o, _ in checks.results)}/{len(checks.results)} checks.**",
            "",
        ]))
    print(f"\n{'PASS' if ok else 'FAIL'}: {sum(o for _, o, _ in checks.results)}/{len(checks.results)} checks "
          "(appended to docs/results.md)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
