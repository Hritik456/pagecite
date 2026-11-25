"""Ingest the pinned RBI corpus into the dedicated eval tenant, through the real pipeline.

    uv run python -m eval.ingest_corpus              # seeds tenant `eval-rbi` if needed, uploads, waits, checks
    uv run python -m eval.ingest_corpus --check      # only the pre-flight checks (what eval.run also does)

1. Every file in eval/corpus.json must exist in corpus/rbi-pdfs/ with the same sha256 (the folder
   has not drifted) and start with %PDF.
2. Tenant `eval-rbi` (its own tenant: eval data never mixes with smoke or load tenants). If it does
   not exist it is seeded with a 60-file batch limit; the key is used for this upload only and never
   printed or stored. If it exists but is incomplete, pass its key (--key / PAGECITE_EVAL_KEY) to
   upload the missing files, or purge it first.
3. One batch through Caddy (API -> presigned PUT to MinIO -> complete), then poll until terminal.
4. Pre-flight: every corpus file has exactly one INDEXED document, and the MySQL chunk count (active
   versions) equals the tenant's count in the Redis index.
"""

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, create_engine, text

from apps.api.config import Settings
from apps.api.search_index import count_tenant, index_config, make_redis

from .build_corpus import PDF_DIR, is_pdf, load_manifest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import smoke_ingest  # noqa: E402  (seed/upload/wait helpers of the P2 smoke run)

TENANT = "eval-rbi"


@dataclass(frozen=True)
class Preflight:
    tenant_id: int
    documents: dict[str, int]  # file name -> INDEXED document id
    problems: list[str]
    mysql_chunks: int
    redis_chunks: int

    @property
    def ok(self) -> bool:
        return not self.problems


def tenant_id_by_name(engine: Engine, name: str) -> int | None:
    with engine.connect() as conn:
        return conn.execute(text("SELECT id FROM tenants WHERE name = :n"), {"n": name}).scalar()


def preflight(engine: Engine, settings: Settings, tenant_id: int, files: list[str]) -> Preflight:
    with engine.connect() as conn:  # one short connection, closed before anything slow happens
        rows = conn.execute(
            text("SELECT filename, id, status FROM documents WHERE tenant_id = :t ORDER BY id"), {"t": tenant_id}
        ).all()
        mysql_chunks = conn.execute(
            text(
                "SELECT COUNT(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
                "WHERE c.tenant_id = :t AND c.version = d.active_version"
            ),
            {"t": tenant_id},
        ).scalar()
    redis_chunks = count_tenant(make_redis(settings.redis_url), index_config(settings), tenant_id)

    indexed: dict[str, list[int]] = {}
    for filename, doc_id, status in rows:
        if status == "INDEXED":
            indexed.setdefault(filename, []).append(doc_id)
    problems = []
    for f in files:
        ids = indexed.get(f, [])
        if len(ids) != 1:
            problems.append(f"{f}: {len(ids)} INDEXED documents (expected exactly 1)")
    extra = sorted(set(indexed) - set(files))
    if extra:
        problems.append(f"{len(extra)} INDEXED documents not in corpus.json (e.g. {extra[0]})")
    if mysql_chunks != redis_chunks:
        problems.append(f"chunk count MySQL {mysql_chunks} != Redis {redis_chunks}: run `flask reindex --tenant {tenant_id}`")
    return Preflight(
        tenant_id=tenant_id,
        documents={f: ids[0] for f, ids in indexed.items() if len(ids) == 1},
        problems=problems,
        mysql_chunks=mysql_chunks,
        redis_chunks=redis_chunks,
    )


def verify_files(pdf_dir: Path, manifest: dict) -> list[str]:
    problems = []
    for entry in manifest["files"]:
        path = pdf_dir / entry["file"]
        if not path.is_file():
            problems.append(f"missing: {entry['file']}")
            continue
        data = path.read_bytes()
        if not is_pdf(data):
            problems.append(f"not a PDF: {entry['file']}")
        elif hashlib.sha256(data).hexdigest() != entry["sha256"]:
            problems.append(f"sha256 differs from corpus.json: {entry['file']}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000/api")
    parser.add_argument("--key", default=os.environ.get("PAGECITE_EVAL_KEY"))
    parser.add_argument("--dir", type=Path, default=PDF_DIR)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--check", action="store_true", help="pre-flight checks only")
    args = parser.parse_args()

    settings = Settings()
    engine = create_engine(settings.database_url)
    manifest = load_manifest()
    files = [f["file"] for f in manifest["files"]]

    tenant_id = tenant_id_by_name(engine, TENANT)
    if not args.check:
        problems = verify_files(args.dir, manifest)
        if problems:
            print("corpus folder does not match eval/corpus.json:\n  " + "\n  ".join(problems))
            return 2
        key = args.key
        if tenant_id is None:
            key = smoke_ingest.seed_tenant(TENANT, max_batch_files=len(files))
            tenant_id = tenant_id_by_name(engine, TENANT)
        have = preflight(engine, settings, tenant_id, files).documents
        missing = [f for f in files if f not in have]
        if missing:
            if key is None:
                print(f"tenant {TENANT} exists but lacks {len(missing)} files; pass --key or purge it first")
                return 2
            print(f"uploading {len(missing)} files to tenant {TENANT} (id {tenant_id}) ...")
            batch_id, _ = smoke_ingest.upload(args.api, key, args.dir, [{"file": f} for f in missing])
            body = smoke_ingest.wait(args.api, key, batch_id, args.timeout)
            failed = [d for d in body["documents"] if d["status"] != "INDEXED"]
            for d in failed:
                print(f"  not INDEXED: document {d['id']} {d['status']}")
    if tenant_id is None:
        print(f"tenant {TENANT} does not exist; run without --check first")
        return 2

    pf = preflight(engine, settings, tenant_id, files)
    print(f"tenant {TENANT} id={tenant_id}: {len(pf.documents)}/{len(files)} files INDEXED, "
          f"chunks MySQL={pf.mysql_chunks} Redis={pf.redis_chunks}")
    for p in pf.problems:
        print(f"  problem: {p}")
    print("PASS" if pf.ok else "FAIL")
    return 0 if pf.ok else 1


if __name__ == "__main__":
    sys.exit(main())
