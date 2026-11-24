"""Chunk stage logic: pages -> ~500-token chunks with ~50 tokens of overlap. Pure and deterministic.

1. Each page is cut into blocks (blank lines), then heading lines and paragraphs.
2. Paragraphs longer than the budget are split by sentence, and sentences longer than that by tokens.
3. Units are packed greedily. A heading always starts a new chunk (without overlap, so a section
   starts clean); other chunk breaks carry the last ~`overlap` tokens of the previous chunk.
4. page_from/page_to cover every unit in the chunk, overlap included, so citations stay honest.
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache

import tiktoken

from .parsing import Page

_NUMBERED = re.compile(r"^\d+(\.\d+)*\.?\s+[A-Z]")
_KEYWORD = re.compile(r"^(chapter|section|article|annex|annexure|appendix|part|schedule)\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_TERMINAL_PUNCT = ".,;:!?"


@dataclass(frozen=True)
class Chunk:
    ord: int
    page_from: int
    page_to: int
    text: str
    token_count: int


@dataclass
class _Unit:
    text: str
    tokens: int
    page: int
    heading: bool = False
    overlap: bool = False


@dataclass
class _Draft:
    units: list[_Unit] = field(default_factory=list)
    tokens: int = 0

    def has_content(self) -> bool:
        return any(not u.overlap for u in self.units)


@lru_cache(maxsize=4)
def get_encoding(name: str) -> tiktoken.Encoding:
    return tiktoken.get_encoding(name)


def _count(enc: tiktoken.Encoding, text: str) -> int:
    # disallowed_special=(): text like "<|endoftext|>" in a document is just text.
    return len(enc.encode(text, disallowed_special=()))


def is_heading_line(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 90:
        return False
    if _NUMBERED.match(line) and len(line.split()) <= 12:
        return True
    if _KEYWORD.match(line) and len(line.split()) <= 12:
        return True
    letters = [c for c in line if c.isalpha()]
    return len(letters) >= 3 and line.upper() == line and line[-1] not in _TERMINAL_PUNCT


def _is_heading_block(lines: list[str]) -> bool:
    """A one-line block that is short and does not end a sentence (e.g. a DOCX heading paragraph)."""
    if len(lines) != 1:
        return False
    line = lines[0].strip()
    return 0 < len(line.split()) <= 12 and line[-1] not in _TERMINAL_PUNCT


def _paragraph_units(enc, text: str, page: int, max_tokens: int) -> list[_Unit]:
    text = " ".join(text.split())
    if not text:
        return []
    tokens = _count(enc, text)
    if tokens <= max_tokens:
        return [_Unit(text, tokens, page)]
    units: list[_Unit] = []
    for sentence in _SENTENCE_END.split(text):
        n = _count(enc, sentence)
        if n <= max_tokens:
            units.append(_Unit(sentence, n, page))
            continue
        ids = enc.encode(sentence, disallowed_special=())
        for start in range(0, len(ids), max_tokens):
            piece = enc.decode(ids[start : start + max_tokens]).strip()
            if piece:
                units.append(_Unit(piece, _count(enc, piece), page))
    return units


def _units(pages: list[Page], enc, max_tokens: int) -> list[_Unit]:
    units: list[_Unit] = []
    for page in pages:
        for block in re.split(r"\n\s*\n", page.text):
            lines = [ln for ln in block.splitlines() if ln.strip()]
            if not lines:
                continue
            if _is_heading_block(lines):
                units.append(_Unit(lines[0].strip(), _count(enc, lines[0].strip()), page.page_no, heading=True))
                continue
            paragraph: list[str] = []
            for line in lines:
                if is_heading_line(line):
                    units.extend(_paragraph_units(enc, " ".join(paragraph), page.page_no, max_tokens))
                    paragraph = []
                    units.append(_Unit(line.strip(), _count(enc, line.strip()), page.page_no, heading=True))
                else:
                    paragraph.append(line)
            units.extend(_paragraph_units(enc, " ".join(paragraph), page.page_no, max_tokens))
    return units


def _tail(enc, units: list[_Unit], overlap: int) -> list[_Unit]:
    """The last ~`overlap` tokens of a chunk, as overlap units (whole units where possible)."""
    if overlap <= 0:
        return []
    tail: list[_Unit] = []
    total = 0
    for unit in reversed(units):
        if total + unit.tokens <= overlap:
            tail.insert(0, _Unit(unit.text, unit.tokens, unit.page, overlap=True))
            total += unit.tokens
            continue
        if not tail:  # the last unit alone is too long: take its final `overlap` tokens
            ids = enc.encode(unit.text, disallowed_special=())
            text = enc.decode(ids[-overlap:]).strip()
            if text:
                tail.append(_Unit(text, _count(enc, text), unit.page, overlap=True))
        break
    return tail


def chunk_pages(
    pages: list[Page], target: int = 500, overlap: int = 50, encoding: str = "cl100k_base"
) -> list[Chunk]:
    if overlap >= target:
        raise ValueError("overlap must be smaller than target")
    enc = get_encoding(encoding)
    # Units are capped at target - overlap, so any unit fits into a chunk that starts with overlap.
    units = _units(pages, enc, max_tokens=target - overlap)

    chunks: list[Chunk] = []
    draft = _Draft()

    def emit(with_overlap: bool) -> None:
        nonlocal draft
        if not draft.has_content():
            return
        text = "\n".join(u.text for u in draft.units)
        pages_in = [u.page for u in draft.units]
        chunks.append(Chunk(len(chunks), min(pages_in), max(pages_in), text, _count(enc, text)))
        carried = _tail(enc, draft.units, overlap) if with_overlap else []
        draft = _Draft(carried, sum(u.tokens for u in carried) + max(len(carried) - 1, 0))

    for unit in units:
        if unit.heading and draft.has_content():
            emit(with_overlap=False)
            draft = _Draft()
        join_cost = 1 if draft.units else 0  # the "\n" between units
        if draft.tokens + join_cost + unit.tokens > target and draft.has_content():
            emit(with_overlap=True)
            join_cost = 1 if draft.units else 0
        if unit.heading and not draft.has_content():
            draft = _Draft()  # a section starts clean: no overlap before its heading
            join_cost = 0
        draft.units.append(unit)
        draft.tokens += join_cost + unit.tokens
    emit(with_overlap=False)
    return chunks
