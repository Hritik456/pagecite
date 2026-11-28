"""No code may FLUSHDB/FLUSHALL except the deliberate P2 reindex check.

In Redis 8 a FLUSHDB of ANY database drops every search index (FT.*), not only that database's keys
(found in P5B: a test fixture flushing the limits DB 15 wiped the chunk index in DB 0). So keeping the
demo limits in their own DB does not protect the index from a flush; clear keys by pattern instead.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {Path("scripts/smoke_ingest.py")}  # --reindex-check: flush DB 0, then prove reindex --all restores it


def test_no_flush_outside_the_reindex_check():
    offenders = []
    for folder in ("apps", "scripts", "deploy", "eval", "load", "tests"):
        for path in (ROOT / folder).rglob("*"):
            if path.suffix not in {".py", ".sh"} or path.relative_to(ROOT) in ALLOWED or path == Path(__file__):
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
                code = line.split("#", 1)[0]
                if re.search(r"\.flush(db|all)\s*\(", code, re.IGNORECASE) or re.search(r"redis-cli.*\bflush(db|all)\b", code, re.IGNORECASE):
                    offenders.append(f"{path.relative_to(ROOT)}:{i}")
    assert offenders == []
