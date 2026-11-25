"""End-to-end smoke run against the running compose stack (the P2 "Done when" check).

    uv run python scripts/smoke_ingest.py                 # upload samples/, wait, verify
    uv run python scripts/smoke_ingest.py --reindex-check # ... then FLUSHDB + reindex --all + compare

1. Tenant: --key / PAGECITE_TENANT_KEY, or a fresh `smoke-<timestamp>` tenant created with
   `docker compose exec api flask seed-tenant` (the key is never printed).
2. Upload samples/ through Caddy (API -> presigned PUT to MinIO -> complete).
3. Poll GET /batches/:id every 2 s until every document is INDEXED or FAILED (timeout).
4. FT.SEARCH each known phrase from samples/manifest.json, scoped to the tenant.
5. MySQL chunk count (current versions) == Redis count for the tenant.
6. --reindex-check: fingerprint the whole index (all tenants), FLUSHDB on Redis DB 0,
   `reindex --all`, fingerprint again; the digest and every tenant's key count must match.

Exit code 0 only if every check passes. Uses real OpenAI embeddings via the workers.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from sqlalchemy import create_engine, text

from apps.api.auth import hash_key
from apps.api.config import Settings
from apps.api.search_index import count_tenant, fingerprint, index_config, make_redis, search_phrase

ROOT = Path(__file__).resolve().parent.parent
TERMINAL = {"INDEXED", "FAILED", "CANCELLED"}
COMPOSE_FLASK = ["docker", "compose", "exec", "-T", "api", "flask", "--app", "apps.api"]


class Checks:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'  - ' + detail if detail else ''}")
        return ok

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.results)


def seed_tenant(name: str | None = None, max_batch_files: int = 50) -> str:
    name = name or f"smoke-{time.strftime('%Y%m%d-%H%M%S')}"
    out = subprocess.run(
        [*COMPOSE_FLASK, "seed-tenant", "--name", name, "--max-file-mb", "25",
         "--max-batch-files", str(max_batch_files)],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    key = next(line.split("=", 1)[1].strip() for line in out.splitlines() if line.startswith("api_key="))
    print(f"seeded tenant {name!r} (key not shown)")
    return key


def upload(api: str, key: str, samples: Path, manifest: list[dict]) -> tuple[int, dict[str, int]]:
    headers = {"X-Tenant-Key": key}
    files = [(m["file"], (samples / m["file"]).read_bytes()) for m in manifest]
    spec = [
        {"name": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest(), "mime": "application/pdf"}
        for n, d in files
    ]
    resp = requests.post(f"{api}/batches", json={"files": spec}, headers=headers, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    ids: dict[str, int] = {}
    for up, (name, data) in zip(body["uploads"], files):
        requests.put(up["presignedUrl"], data=data, headers=up["headers"], timeout=60).raise_for_status()
        requests.post(f"{api}/documents/{up['documentId']}/complete", headers=headers, timeout=30).raise_for_status()
        ids[name] = up["documentId"]
    print(f"uploaded {len(ids)} files -> batch {body['batchId']}")
    return body["batchId"], ids


def wait(api: str, key: str, batch_id: int, timeout_s: int) -> dict:
    deadline = time.monotonic() + timeout_s
    last = ""
    while True:
        body = requests.get(f"{api}/batches/{batch_id}", headers={"X-Tenant-Key": key}, timeout=30).json()
        statuses = [d["status"] for d in body["documents"]]
        summary = " ".join(f"{s}={statuses.count(s)}" for s in sorted(set(statuses)))
        if summary != last:
            print(f"  t+{timeout_s - (deadline - time.monotonic()):5.1f}s  batch={body['batch']['status']}  {summary}")
            last = summary
        if all(s in TERMINAL for s in statuses) or time.monotonic() > deadline:
            return body
        time.sleep(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000/api")
    parser.add_argument("--key", default=os.environ.get("PAGECITE_TENANT_KEY"))
    parser.add_argument("--samples", type=Path, default=ROOT / "samples")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--reindex-check", action="store_true")
    args = parser.parse_args()

    settings = Settings()  # host-side values from .env (localhost)
    cfg = index_config(settings)
    r = make_redis(settings.redis_url)
    engine = create_engine(settings.database_url)
    manifest = json.loads((args.samples / "manifest.json").read_text())
    checks = Checks()

    key = args.key or seed_tenant()
    with engine.connect() as conn:
        tenant_id = conn.execute(text("SELECT id FROM tenants WHERE api_key_hash = :h"), {"h": hash_key(key)}).scalar()
    if tenant_id is None:
        print("unknown tenant key")
        return 2

    print("\n== ingest")
    batch_id, ids = upload(args.api, key, args.samples, manifest)
    body = wait(args.api, key, batch_id, args.timeout)

    print("\n== checks")
    by_id = {d["id"]: d for d in body["documents"]}
    for item in manifest:
        doc = by_id[ids[item["file"]]]
        stages = ",".join(f"{s['stage']}={s['status']}" for s in doc["stages"])
        checks.add(f"{item['file']} INDEXED", doc["status"] == "INDEXED", f"{doc['status']} ({stages})")
    checks.add("batch DONE", body["batch"]["status"] == "DONE", body["batch"]["status"])

    with engine.connect() as conn:
        scanned = ids[next(m["file"] for m in manifest if m["scanned"])]
        ocr_pages = conn.execute(
            text("SELECT COALESCE(SUM(ocr), 0) FROM document_pages WHERE document_id = :d"), {"d": scanned}
        ).scalar()
        mysql_chunks = conn.execute(
            text(
                "SELECT COUNT(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
                "WHERE c.tenant_id = :t AND c.version = d.active_version"  # the served version (docs/adr/0004)
            ),
            {"t": tenant_id},
        ).scalar()
    checks.add("scanned sample went through OCR", ocr_pages >= 1, f"{ocr_pages} OCR page(s)")

    for item in manifest:
        hits = search_phrase(r, cfg, tenant_id, item["phrase"])
        checks.add(f"FT.SEARCH '{item['phrase']}'", hits == [ids[item["file"]]], f"hits={hits}")

    redis_chunks = count_tenant(r, cfg, tenant_id)
    checks.add("chunk count MySQL == Redis", mysql_chunks == redis_chunks, f"mysql={mysql_chunks} redis={redis_chunks}")

    if args.reindex_check:
        print("\n== reindex check (FLUSHDB clears Redis DB 0 = the index for EVERY tenant; reindex --all restores it)")
        before = fingerprint(r, cfg)
        print(f"  before: keys={before.keys} tenants={before.per_tenant} sha256={before.digest[:16]}...")
        r.flushdb()
        print(f"  after FLUSHDB: keys={fingerprint(r, cfg).keys}")
        out = subprocess.run([*COMPOSE_FLASK, "reindex", "--all"], cwd=ROOT, capture_output=True, text=True)
        print("  " + out.stdout.strip().replace("\n", "\n  "))
        after = fingerprint(r, cfg)
        print(f"  after:  keys={after.keys} tenants={after.per_tenant} sha256={after.digest[:16]}...")
        checks.add("reindex --all exit code", out.returncode == 0, out.stderr.strip()[-200:])
        checks.add("per-tenant key counts identical", after.per_tenant == before.per_tenant)
        checks.add("index fingerprint identical (all tenants)", after.digest == before.digest)
        checks.add("index definition recreated", count_tenant(r, cfg, tenant_id) == redis_chunks)

    passed = sum(ok for _, ok, _ in checks.results)
    print(f"\n{'PASS' if checks.ok else 'FAIL'}: {passed}/{len(checks.results)} checks")
    return 0 if checks.ok else 1


if __name__ == "__main__":
    sys.exit(main())
