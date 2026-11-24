"""Generate the sample corpus in samples/: 4 text PDFs + 1 scanned (image-only) PDF + manifest.json.

Each document hides one unusual "known phrase" on a known page, so tests and the smoke run can check
that FT.SEARCH finds it in the right document. Output is deterministic (fixed dates, seeded text),
so re-running does not change the committed files.

    uv run python scripts/make_samples.py
"""

import json
import random
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "samples"
FIXED_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)

SUBJECTS = [
    "the applicant", "each department", "the reviewing officer", "every member of staff",
    "the finance committee", "the registrar", "a regional office", "the compliance unit",
]
VERBS = [
    "shall submit", "must retain", "may request", "is required to verify", "should forward",
    "will publish", "must record", "shall review",
]
OBJECTS = [
    "a signed declaration", "the supporting documents", "a written justification",
    "the quarterly summary", "an updated register", "the approval memo", "a copy of the notice",
    "the relevant ledger entries",
]
TAILS = [
    "within fifteen working days of the request.", "before the end of the financial year.",
    "in the format prescribed in the annexure.", "unless an exemption has been granted in writing.",
    "and keep a copy for at least five years.", "through the designated online portal.",
    "after consulting the head of the unit.", "so that the audit trail remains complete.",
]

DOCS = [
    {
        "file": "01-leave-policy.pdf",
        "title": "LEAVE POLICY CIRCULAR",
        "sections": ["1. Purpose", "2. Eligibility", "3. Application Procedure", "4. Approval and Records"],
        "phrase": "cobalt heron allowance",
        "phrase_section": 2,
    },
    {
        "file": "02-procurement-manual.pdf",
        "title": "PROCUREMENT MANUAL",
        "sections": ["1. Scope", "2. Thresholds", "3. Tender Evaluation", "4. Contract Award", "5. Records"],
        "phrase": "quartz meridian clause",
        "phrase_section": 3,
    },
    {
        "file": "03-data-retention.pdf",
        "title": "DATA RETENTION GUIDELINES",
        "sections": ["1. Introduction", "2. Retention Periods", "3. Secure Disposal", "4. Exceptions"],
        "phrase": "saffron lattice protocol",
        "phrase_section": 1,
    },
    {
        "file": "04-travel-rules.pdf",
        "title": "TRAVEL AND REIMBURSEMENT RULES",
        "sections": ["1. General", "2. Modes of Travel", "3. Daily Allowance", "4. Claims Settlement"],
        "phrase": "obsidian tidewater circular",
        "phrase_section": 3,
    },
]

SCANNED = {
    "file": "05-scanned-notice.pdf",
    "title": "OFFICE NOTICE",
    "phrase": "granite lantern notice",
    "lines": [
        "OFFICE NOTICE",
        "",
        "All units are informed that the annual",
        "inventory check will begin next month.",
        "Heads of units must confirm the list of",
        "assets held by their teams.",
        "",
        "This is the granite lantern notice for",
        "the current year and replaces all",
        "earlier instructions on the subject.",
    ],
}


def paragraph(rng: random.Random, sentences: int) -> str:
    return " ".join(
        f"{rng.choice(SUBJECTS).capitalize()} {rng.choice(VERBS)} {rng.choice(OBJECTS)} {rng.choice(TAILS)}"
        for _ in range(sentences)
    )


def text_pdf(spec: dict, seed: int) -> int:
    """Write a multi-page text PDF. Returns the page the known phrase landed on."""
    rng = random.Random(seed)
    pdf = FPDF(format="A4")
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_title(spec["title"])
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 10, spec["title"], new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    phrase_page = 0
    for i, heading in enumerate(spec["sections"]):
        pdf.set_font("Helvetica", "B", 13)
        pdf.multi_cell(0, 8, heading, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 11)
        for p in range(3):
            body = paragraph(rng, rng.randint(5, 8))
            has_phrase = i == spec["phrase_section"] and p == 1
            if has_phrase:
                body += f" This provision is known internally as the {spec['phrase']}."
            pdf.multi_cell(0, 6, body, new_x="LMARGIN", new_y="NEXT")
            if has_phrase:  # the phrase ends the paragraph, so it sits on the page the paragraph ends on
                phrase_page = pdf.page_no()
            pdf.ln(3)
    pdf.output(str(OUT / spec["file"]))
    return phrase_page


def scanned_pdf(spec: dict) -> None:
    """An image-only page (no text layer), like a scan: forces the OCR path."""
    width, height = 1240, 1754  # A4 at 150 dpi
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=40)
    y = 160
    for line in spec["lines"]:
        draw.text((110, y), line, fill=0, font=font)
        y += 70
    img.save(
        OUT / spec["file"], "PDF", resolution=150.0,
        title=spec["title"], creationDate=FIXED_DATE.timetuple(), modDate=FIXED_DATE.timetuple(),
    )


def main() -> None:
    OUT.mkdir(exist_ok=True)
    manifest = []
    for n, spec in enumerate(DOCS):
        page = text_pdf(spec, seed=1000 + n)
        manifest.append({"file": spec["file"], "phrase": spec["phrase"], "page": page, "scanned": False})
    scanned_pdf(SCANNED)
    manifest.append({"file": SCANNED["file"], "phrase": SCANNED["phrase"], "page": 1, "scanned": True})
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for item in manifest:
        size = (OUT / item["file"]).stat().st_size
        print(textwrap.shorten(f"{item['file']:28s} {size:>7d} B  page {item['page']}  '{item['phrase']}'", 100))


if __name__ == "__main__":
    main()
