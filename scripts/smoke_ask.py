"""P4 "Done when" check against the running compose stack, with the real models.

    uv run python scripts/smoke_ask.py                  # seeds a tenant + ingests samples/ if needed
    uv run python scripts/smoke_ask.py --key pc_...     # reuse a tenant that already has the samples

For every question in samples/questions.json it calls POST /api/ask (SSE), prints the answer and its
citations, and checks:
- answerable: not abstained, at least one citation, the expected file + page among the cited blocks,
  every `must_include` present and every `must_not_include` absent (the prompt-injection question);
- not answerable: exactly "Not found in the documents." with no citations.
Then it prints that request's token lines from the API log (decompose / embed_query / rerank / answer).
The key is never printed. Exit code 0 only if every check passes.
"""

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import requests
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke_ingest  # noqa: E402  (sibling script: seed/upload/wait helpers)

from apps.api.auth import hash_key  # noqa: E402
from apps.api.config import Settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ABSTAIN = "Not found in the documents."


def ask(api: str, key: str, question: str) -> dict:
    started = time.perf_counter()
    resp = requests.post(
        f"{api}/ask", json={"question": question, "stream": True},
        headers={"X-Tenant-Key": key, "Accept": "text/event-stream"}, stream=True, timeout=(5, 120),
    )
    out = {"answer": "", "citations": [], "abstained": None, "error": None,
           "request_id": resp.headers.get("X-Request-ID"), "first_token_s": None}
    if resp.status_code != 200:
        out["error"] = resp.json().get("error")
        return out
    event, data = None, []
    for line in resp.iter_lines(decode_unicode=True):
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data.append(line[6:])
        elif line == "" and event:
            payload = json.loads("\n".join(data))
            if event == "token":
                out["first_token_s"] = out["first_token_s"] or round(time.perf_counter() - started, 2)
                out["answer"] += payload["text"]
            elif event == "citations":
                out["citations"], out["abstained"] = payload["citations"], payload["abstained"]
            elif event == "error":
                out["error"] = payload
            event, data = None, []
    out["total_s"] = round(time.perf_counter() - started, 2)
    return out


def token_lines(request_id: str) -> list[str]:
    logs = subprocess.run(
        ["docker", "compose", "logs", "api", "--since", "15m", "--no-log-prefix"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    keep = []
    for line in logs.splitlines():
        if f"request_id={request_id}" in line and ("llm_call" in line or "ask_done" in line):
            keep.append(line.split(" pagecite.ask ", 1)[-1])
    return keep


def ensure_corpus(api: str, key: str, tenant_id: int, engine, samples: Path, timeout_s: int) -> dict[str, int]:
    """file name -> document id of the tenant's INDEXED copy; uploads samples/ first if missing."""
    def indexed() -> dict[str, int]:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT filename, MAX(id) FROM documents WHERE tenant_id = :t AND status = 'INDEXED' GROUP BY filename"),
                {"t": tenant_id},
            ).all()
        return {name: doc_id for name, doc_id in rows}

    manifest = json.loads((samples / "manifest.json").read_text())
    have = indexed()
    if not all(m["file"] in have for m in manifest):
        print("ingesting samples/ for this tenant ...")
        batch_id, _ = smoke_ingest.upload(api, key, samples, manifest)
        smoke_ingest.wait(api, key, batch_id, timeout_s)
        have = indexed()
    return have


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000/api")
    parser.add_argument("--key", default=os.environ.get("PAGECITE_TENANT_KEY"))
    parser.add_argument("--samples", type=Path, default=ROOT / "samples")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    settings = Settings()
    engine = create_engine(settings.database_url)
    key = args.key or smoke_ingest.seed_tenant()
    with engine.connect() as conn:
        tenant_id = conn.execute(text("SELECT id FROM tenants WHERE api_key_hash = :h"), {"h": hash_key(key)}).scalar()
    if tenant_id is None:
        print("unknown tenant key")
        return 2
    docs = ensure_corpus(args.api, key, tenant_id, engine, args.samples, args.timeout)
    questions = json.loads((args.samples / "questions.json").read_text())
    checks = smoke_ingest.Checks()

    for q in questions:
        print(f"\n=== {q['id']}: {q['question']}")
        r = ask(args.api, key, q["question"])
        if r["error"]:
            checks.add(f"{q['id']} no error", False, json.dumps(r["error"]))
            continue
        print(textwrap.indent(textwrap.fill(r["answer"], 100), "  > "))
        for c in r["citations"]:
            pages = f"p.{c['page']}" if c["page"] == c["page_to"] else f"pp.{c['page']}-{c['page_to']}"
            print(f"    [{c['n']}] {c['title']}, {pages} (document {c['document_id']})")
        print(f"    first token {r['first_token_s']} s, total {r['total_s']} s, request {r['request_id']}")
        for line in token_lines(r["request_id"]):
            print(f"    log: {line}")

        answer = r["answer"].lower()
        if not q["answerable"]:
            checks.add(f"{q['id']} abstains exactly", r["answer"].strip() == ABSTAIN and not r["citations"],
                       repr(r["answer"][:80]))
            continue
        checks.add(f"{q['id']} answered (not abstained)", r["abstained"] is False)
        checks.add(f"{q['id']} cited", len(r["citations"]) > 0, f"{len(r['citations'])} citation(s)")
        for exp in q["expected"]:
            doc_id = docs.get(exp["file"])
            hit = any(c["document_id"] == doc_id and c["page"] <= exp["page"] <= c["page_to"] for c in r["citations"])
            checks.add(f"{q['id']} cites {exp['file']} p.{exp['page']}", hit)
        for needle in q["must_include"]:
            checks.add(f"{q['id']} mentions {needle!r}", needle.lower() in answer)
        for needle in q["must_not_include"]:
            checks.add(f"{q['id']} does not say {needle!r}", needle.lower() not in answer)

    passed = sum(ok for _, ok, _ in checks.results)
    print(f"\n{'PASS' if checks.ok else 'FAIL'}: {passed}/{len(checks.results)} checks")
    return 0 if checks.ok else 1


if __name__ == "__main__":
    sys.exit(main())
