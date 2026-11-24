"""Parse stage logic: bytes -> text per page. Pure apart from the injected OCR function.

PDF: pdfplumber text layer per page; a page with (almost) no text is treated as scanned and OCR'd.
DOCX: python-docx paragraphs; pages come from explicit and last-rendered page breaks, which is an
approximation (DOCX has no fixed pagination).
"""

import io
import logging
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass

import docx
import pdfplumber
from docx.oxml.ns import qn
from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect
from PIL.Image import Image

from apps.api.quotas import DOCX, PDF

from .errors import PermanentError

# pdfminer warns about every malformed font descriptor (harmless; text still extracts). Keep logs useful.
logging.getLogger("pdfminer").setLevel(logging.ERROR)

# Fewer text-layer characters than this means "scanned page".
MIN_TEXT_CHARS = 20
OCR_RESOLUTION = 300

OcrFn = Callable[[Image, int], str]  # (page image, timeout seconds) -> text


@dataclass(frozen=True)
class Page:
    page_no: int
    text: str
    ocr: bool = False


@dataclass(frozen=True)
class OcrLimits:
    max_pages: int = 50
    page_timeout_s: int = 60
    budget_s: int = 900


def parse_document(
    data: bytes,
    mime: str,
    ocr_fn: OcrFn,
    limits: OcrLimits = OcrLimits(),
    clock: Callable[[], float] = time.monotonic,
) -> list[Page]:
    if mime == PDF:
        pages = parse_pdf(data, ocr_fn, limits, clock)
    elif mime == DOCX:
        pages = parse_docx(data)
    else:
        raise PermanentError("UNSUPPORTED_TYPE", f"cannot parse {mime}")
    if not any(p.text.strip() for p in pages):
        raise PermanentError("NO_TEXT", "the document has no extractable text")
    return pages


def _classify_pdf_error(exc: BaseException) -> PermanentError:
    seen: BaseException | None = exc
    while seen is not None:
        candidates = [seen, *(a for a in seen.args if isinstance(a, BaseException))]
        if any(isinstance(c, (PDFEncryptionError, PDFPasswordIncorrect)) for c in candidates):
            return PermanentError("ENCRYPTED", "the PDF is encrypted")
        seen = seen.__cause__ or seen.__context__
    return PermanentError("CORRUPT", f"the PDF cannot be read ({type(exc).__name__})")


def parse_pdf(data: bytes, ocr_fn: OcrFn, limits: OcrLimits, clock: Callable[[], float]) -> list[Page]:
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:  # pdfplumber wraps every pdfminer error in PdfminerException
        raise _classify_pdf_error(exc) from exc

    with pdf:
        try:
            texts = [(page.extract_text() or "").strip() for page in pdf.pages]
        except Exception as exc:
            raise _classify_pdf_error(exc) from exc

        scanned = [i for i, text in enumerate(texts) if len(text) < MIN_TEXT_CHARS]
        # Checked before any OCR runs: a huge scan fails fast instead of eating the time budget.
        if len(scanned) > limits.max_pages:
            raise PermanentError(
                "OCR_PAGE_LIMIT", f"{len(scanned)} scanned pages; the limit is {limits.max_pages}"
            )

        pages = [Page(page_no=i + 1, text=text) for i, text in enumerate(texts)]
        deadline = clock() + limits.budget_s
        for i in scanned:
            if clock() >= deadline:
                raise PermanentError("OCR_TIME_BUDGET", f"OCR exceeded {limits.budget_s} s")
            image = pdf.pages[i].to_image(resolution=OCR_RESOLUTION).original
            pages[i] = Page(page_no=i + 1, text=ocr_fn(image, limits.page_timeout_s).strip(), ocr=True)
    return pages


_BREAK_TAGS = {qn("w:br"), qn("w:lastRenderedPageBreak")}
_TEXT_TAG, _TAB_TAG = qn("w:t"), qn("w:tab")


def parse_docx(data: bytes) -> list[Page]:
    try:
        document = docx.Document(io.BytesIO(data))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise PermanentError("CORRUPT", f"the DOCX cannot be read ({type(exc).__name__})") from exc

    pages: list[list[str]] = [[]]
    text_since_break = False

    # Each paragraph becomes its own block (blank line between), so a short heading paragraph
    # is recognised by the chunker's heading rule.
    for para in document.paragraphs:
        parts: list[str] = []

        def flush_paragraph() -> None:
            line = "".join(parts).strip()
            if line:
                pages[-1].append(line)
            parts.clear()

        for el in para._p.iter():
            if el.tag == _TEXT_TAG and el.text:
                parts.append(el.text)
                text_since_break = True
            elif el.tag == _TAB_TAG:
                parts.append("\t")
            elif el.tag in _BREAK_TAGS:
                if el.tag == qn("w:br") and el.get(qn("w:type")) != "page":
                    parts.append("\n")  # a plain line break
                    continue
                # Word often writes an explicit break and a rendered break together: count them once.
                if text_since_break:
                    flush_paragraph()
                    pages.append([])
                    text_since_break = False
        flush_paragraph()

    return [Page(page_no=i + 1, text="\n\n".join(lines).strip()) for i, lines in enumerate(pages)]
