"""Build eval/corpus.json from the downloaded RBI PDFs (corpus/rbi-pdfs/, not committed).

    uv run python -m eval.build_corpus            # writes eval/corpus.json
    uv run python -m eval.build_corpus --check    # verifies the folder still matches it

Only files whose first bytes are %PDF are listed. Anything else (13697_... is an HTML error page
saved with a .PDF name) is reported and excluded. corpus.json is committed: it pins the corpus
(sha256 + page count per file), so CI and the golden-set tests can check rows against it without
the PDFs, and ingest_corpus.py refuses a folder that has drifted.
"""

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = ROOT / "corpus" / "rbi-pdfs"
MANIFEST = ROOT / "eval" / "corpus.json"


def is_pdf(data: bytes) -> bool:
    return data[:4] == b"%PDF"


def scan(pdf_dir: Path) -> tuple[list[dict], list[str]]:
    files, excluded = [], []
    for path in sorted(p for p in pdf_dir.iterdir() if p.is_file()):
        data = path.read_bytes()
        if not is_pdf(data):
            excluded.append(path.name)
            continue
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = len(pdf.pages)
        files.append({
            "file": path.name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "pages": pages,
        })
    return files, excluded


def load_manifest(path: Path = MANIFEST) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, default=PDF_DIR)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    files, excluded = scan(args.dir)
    for name in excluded:
        print(f"excluded (not a PDF: does not start with %PDF): {name}")
    if args.check:
        want = load_manifest()["files"]
        ok = files == want
        print(f"{'OK' if ok else 'MISMATCH'}: folder has {len(files)} PDFs, corpus.json lists {len(want)}")
        return 0 if ok else 1
    manifest = {
        "source": "RBI notifications (rbi.org.in), downloaded by corpus/download-rbi-pdfs.ps1",
        "n_files": len(files),
        "n_pages": sum(f["pages"] for f in files),
        "excluded": excluded,
        "files": files,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {MANIFEST.relative_to(ROOT)}: {len(files)} PDFs, {manifest['n_pages']} pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
