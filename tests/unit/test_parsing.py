import io
import json
import shutil
from pathlib import Path

import docx
import pytest
from docx.enum.text import WD_BREAK
from fpdf import FPDF

from apps.api.quotas import DOCX, PDF
from apps.worker.errors import PermanentError
from apps.worker.parsing import OcrLimits, parse_document

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
MANIFEST = json.loads((SAMPLES / "manifest.json").read_text())


def flat(text: str) -> str:
    return " ".join(text.split())


class StubOcr:
    def __init__(self, text: str = "stub ocr text for a scanned page"):
        self.text = text
        self.calls = 0

    def __call__(self, image, timeout):
        self.calls += 1
        assert image.size[0] > 1000  # rendered at OCR resolution
        assert timeout > 0
        return self.text


def no_ocr(image, timeout):
    raise AssertionError("OCR must not run for pages with a text layer")


@pytest.mark.parametrize("item", [m for m in MANIFEST if not m["scanned"]], ids=lambda m: m["file"])
def test_text_pdf_pages_and_known_phrase(item):
    pages = parse_document((SAMPLES / item["file"]).read_bytes(), PDF, no_ocr)
    assert [p.page_no for p in pages] == list(range(1, len(pages) + 1))
    assert not any(p.ocr for p in pages)
    hits = [p.page_no for p in pages if item["phrase"] in flat(p.text)]
    assert hits == [item["page"]]


def test_scanned_pdf_goes_through_ocr_only_for_the_image_page():
    item = next(m for m in MANIFEST if m["scanned"])
    ocr = StubOcr()
    pages = parse_document((SAMPLES / item["file"]).read_bytes(), PDF, ocr)
    assert ocr.calls == 1
    assert pages[0].ocr is True
    assert pages[0].text == ocr.text


def make_pdf(pages: list[str]) -> bytes:
    pdf = FPDF()
    pdf.set_font("Helvetica", size=12)
    for text in pages:
        pdf.add_page()
        if text:
            pdf.multi_cell(0, 8, text)
    return bytes(pdf.output())


def test_mixed_pdf_ocrs_only_pages_without_text():
    ocr = StubOcr()
    data = make_pdf(["A page with a real text layer, long enough to count.", "", "Another real text page here."])
    pages = parse_document(data, PDF, ocr)
    assert ocr.calls == 1
    assert [p.ocr for p in pages] == [False, True, False]


def test_too_many_scanned_pages_fails_before_any_ocr():
    ocr = StubOcr()
    with pytest.raises(PermanentError) as exc:
        parse_document(make_pdf(["", "", ""]), PDF, ocr, OcrLimits(max_pages=2))
    assert exc.value.code == "OCR_PAGE_LIMIT"
    assert ocr.calls == 0


def test_ocr_time_budget_is_enforced():
    now = [0.0]

    def clock():
        return now[0]

    def slow_ocr(image, timeout):
        now[0] += 40  # each page "takes" 40 s
        return "text"

    with pytest.raises(PermanentError) as exc:
        parse_document(make_pdf(["", "", "", ""]), PDF, slow_ocr, OcrLimits(budget_s=100), clock=clock)
    assert exc.value.code == "OCR_TIME_BUDGET"
    assert now[0] == 120  # pages 1-3 ran; page 4 was refused at the deadline


def test_encrypted_pdf_is_permanent():
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text="secret")
    pdf.set_encryption(owner_password="owner", user_password="user")
    with pytest.raises(PermanentError) as exc:
        parse_document(bytes(pdf.output()), PDF, no_ocr)
    assert exc.value.code == "ENCRYPTED"


@pytest.mark.parametrize("data", [b"", b"not a pdf at all", (SAMPLES / "01-leave-policy.pdf").read_bytes()[:900]])
def test_corrupt_pdf_is_permanent(data):
    with pytest.raises(PermanentError) as exc:
        parse_document(data, PDF, no_ocr)
    assert exc.value.code == "CORRUPT"


def test_pdf_without_any_text_is_permanent():
    with pytest.raises(PermanentError) as exc:
        parse_document(make_pdf([""]), PDF, StubOcr(text="   "))
    assert exc.value.code == "NO_TEXT"


def test_unsupported_type_is_permanent():
    with pytest.raises(PermanentError) as exc:
        parse_document(b"hello", "text/plain", no_ocr)
    assert exc.value.code == "UNSUPPORTED_TYPE"


def make_docx() -> bytes:
    d = docx.Document()
    d.add_heading("Introduction", level=1)
    d.add_paragraph("First page paragraph about the policy.")
    run = d.add_paragraph("Still on page one.").add_run()
    run.add_break(WD_BREAK.PAGE)
    d.add_heading("Scope", level=1)
    d.add_paragraph("Second page paragraph.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_docx_pages_follow_page_breaks():
    pages = parse_document(make_docx(), DOCX, no_ocr)
    assert len(pages) == 2
    assert "Introduction" in pages[0].text and "Still on page one." in pages[0].text
    assert pages[1].text.startswith("Scope")
    assert "Second page paragraph." in pages[1].text


def test_corrupt_docx_is_permanent():
    with pytest.raises(PermanentError) as exc:
        parse_document(b"PK\x03\x04 not really a zip", DOCX, no_ocr)
    assert exc.value.code == "CORRUPT"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed on this machine")
def test_real_tesseract_reads_the_scanned_sample():
    from apps.worker.context import tesseract_ocr

    item = next(m for m in MANIFEST if m["scanned"])
    pages = parse_document((SAMPLES / item["file"]).read_bytes(), PDF, tesseract_ocr)
    assert item["phrase"] in flat(pages[0].text).lower()
