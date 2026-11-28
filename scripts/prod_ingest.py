"""Ingest the 60 pinned RBI PDFs into the prod demo tenant, through the public API only (P5B).

    $env:PAGECITE_ADMIN_KEY = "<the demo-rbi tenant's full key>"     # PowerShell; never printed or stored
    uv run python scripts/prod_ingest.py                              # https://pagecite.duckdns.org/api
    uv run python scripts/prod_ingest.py --api https://pagecite.duckdns.org/api --timeout 1800

1. Verifies every file in corpus/rbi-pdfs/ against eval/corpus.json (sha256, %PDF): the same corpus as
   the eval.
2. One batch: POST /batches, then PUT to the presigned S3 URL + POST /complete with 4 threads.
   (From this laptop the PUT is not a browser request, so the bucket CORS does not apply here.)
3. Polls GET /batches/:id every 5 s, retrying transient errors, until every document is terminal.
4. Prints throughput and run time per stage from the stages' startedAt/finishedAt (server clock).

On the server, while it runs: `sudo /opt/pagecite/sample-memory.sh`, and afterwards
`sudo /opt/pagecite/oom-check.sh` (docs/runbook.md, "Prod ingest").
"""

import argparse
import hashlib
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from eval.build_corpus import PDF_DIR, load_manifest  # noqa: E402
from eval.ingest_corpus import verify_files  # noqa: E402

TERMINAL = {"INDEXED", "FAILED", "CANCELLED"}
STAGES = ("parse", "chunk", "embed", "index")


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, round(p / 100 * len(v) + 0.5) - 1))]


def get_with_retry(url: str, headers: dict, tries: int = 6) -> dict:
    for attempt in range(tries):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp.json()
        except requests.RequestException:
            if attempt == tries - 1:
                raise
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url} kept failing")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="https://pagecite.duckdns.org/api")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    key = os.environ.get("PAGECITE_ADMIN_KEY", "").strip()
    if not key.startswith("pc_"):
        print("set PAGECITE_ADMIN_KEY to the demo tenant's full key (pc_...); an ask-only key can't upload")
        return 2
    manifest = load_manifest()
    problems = verify_files(PDF_DIR, manifest)
    if problems:
        print("corpus folder does not match eval/corpus.json:\n  " + "\n  ".join(problems))
        return 2
    files = [f["file"] for f in manifest["files"]]
    data = {f: (PDF_DIR / f).read_bytes() for f in files}
    headers = {"X-Tenant-Key": key}

    spec = [{"name": f, "size": len(data[f]), "sha256": hashlib.sha256(data[f]).hexdigest(),
             "mime": "application/pdf"} for f in files]
    started = time.monotonic()
    resp = requests.post(f"{args.api}/batches", json={"files": spec}, headers=headers, timeout=60)
    if resp.status_code != 201:
        print(f"POST /batches: HTTP {resp.status_code} {resp.json().get('error', {}).get('code')}")
        return 1
    body = resp.json()

    def one(pair):
        up, name = pair
        requests.put(up["presignedUrl"], data=data[name], headers=up["headers"], timeout=180).raise_for_status()
        r = requests.post(f"{args.api}/documents/{up['documentId']}/complete", headers=headers, timeout=60)
        r.raise_for_status()

    with ThreadPoolExecutor(args.threads) as pool:
        list(pool.map(one, zip(body["uploads"], files)))
    upload_s = time.monotonic() - started
    print(f"batch {body['batchId']}: {len(files)} uploads + completes in {upload_s:.1f} s", flush=True)

    deadline = time.monotonic() + args.timeout
    last = ""
    while True:
        view = get_with_retry(f"{args.api}/batches/{body['batchId']}", headers)
        statuses = [d["status"] for d in view["documents"]]
        line = " ".join(f"{s}={statuses.count(s)}" for s in sorted(set(statuses)))
        if line != last:
            print(f"  t+{time.monotonic() - started:6.1f}s  {line}", flush=True)
            last = line
        if all(s in TERMINAL for s in statuses) or time.monotonic() > deadline:
            break
        time.sleep(5)

    docs = view["documents"]
    indexed = sum(d["status"] == "INDEXED" for d in docs)
    runs: dict[str, list[float]] = {s: [] for s in STAGES}
    starts, ends = [], []
    for d in docs:
        for st in d.get("stages", []):
            if st["startedAt"] and st["finishedAt"]:
                a, b = datetime.fromisoformat(st["startedAt"]), datetime.fromisoformat(st["finishedAt"])
                runs[st["stage"]].append((b - a).total_seconds())
                starts.append(a)
                ends.append(b)
    span = (max(ends) - min(starts)).total_seconds() if starts else None
    print(f"\n{indexed}/{len(docs)} INDEXED; batch {view['batch']['status']}; "
          f"pipeline span {span:.0f} s (first stage start -> last stage end)" if span else "no stage timings")
    if span:
        print(f"throughput: {indexed / (span / 60):.1f} documents/min")
    for s in STAGES:
        v = runs[s]
        if v:
            print(f"  {s:<6} n={len(v):<3} p50={pct(v, 50):.1f}s p95={pct(v, 95):.1f}s max={max(v):.1f}s")
    retried = sum(1 for d in docs for st in d.get("stages", []) if st["attempts"] > 1)
    print(f"stages that needed a retry: {retried}; failed documents: {sum(d['status'] == 'FAILED' for d in docs)}")
    return 0 if indexed == len(docs) else 1


if __name__ == "__main__":
    sys.exit(main())
