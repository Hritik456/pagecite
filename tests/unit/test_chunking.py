import pytest

from apps.worker.chunking import chunk_pages, get_encoding, is_heading_line
from apps.worker.parsing import Page

ENC = get_encoding("cl100k_base")


def words(n: int, tag: str) -> str:
    """n distinct-ish sentences of ~10 tokens each, tagged so tests can find them."""
    return " ".join(f"The {tag} clause number {i} applies to every unit here." for i in range(n))


def tokens(text: str) -> int:
    return len(ENC.encode(text))


def test_empty_input_gives_no_chunks():
    assert chunk_pages([Page(1, ""), Page(2, "   ")]) == []


def test_short_document_is_one_chunk_with_its_page():
    chunks = chunk_pages([Page(3, "A short page of text.")])
    assert len(chunks) == 1
    assert (chunks[0].ord, chunks[0].page_from, chunks[0].page_to) == (0, 3, 3)
    assert chunks[0].token_count == tokens(chunks[0].text)


def test_page_mapping_spans_a_page_break():
    pages = [Page(1, words(10, "alpha")), Page(2, words(10, "beta"))]
    chunks = chunk_pages(pages, target=500, overlap=50)
    assert len(chunks) == 1
    assert (chunks[0].page_from, chunks[0].page_to) == (1, 2)


def test_chunks_follow_pages_in_order():
    pages = [Page(p, words(40, f"page{p}")) for p in range(1, 6)]
    chunks = chunk_pages(pages, target=200, overlap=30)
    assert [c.ord for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert c.page_from <= c.page_to
        for p in range(1, 6):
            if f"page{p} clause" in c.text:
                assert c.page_from <= p <= c.page_to
    assert chunks[0].page_from == 1
    assert chunks[-1].page_to == 5


def test_chunk_size_stays_within_target():
    chunks = chunk_pages([Page(1, words(400, "gamma"))], target=200, overlap=30)
    assert len(chunks) > 5
    assert all(c.token_count <= 200 + 5 for c in chunks), [c.token_count for c in chunks]


def test_consecutive_chunks_overlap():
    chunks = chunk_pages([Page(1, words(200, "delta"))], target=150, overlap=30)
    assert len(chunks) > 3
    for prev, nxt in zip(chunks, chunks[1:]):
        prev_lines, next_lines = prev.text.split("\n"), nxt.text.split("\n")
        # the next chunk opens with the previous chunk's last k lines (k >= 1), ~30 tokens in total
        k = next(k for k in range(len(next_lines), 0, -1) if next_lines[:k] == prev_lines[-k:])
        carried = "\n".join(next_lines[:k])
        assert 0 < len(ENC.encode(carried)) <= 30 + 2


def test_heading_starts_a_new_chunk_without_overlap():
    text = words(20, "intro") + "\n2. Eligibility\n" + words(5, "elig")
    chunks = chunk_pages([Page(1, text)], target=500, overlap=50)
    assert len(chunks) == 2
    assert chunks[1].text.startswith("2. Eligibility")
    assert "intro" not in chunks[1].text


def test_heading_block_from_docx_style_paragraphs():
    text = "Introduction\n\nThis policy explains the rules. It applies to all staff.\n\nScope\n\nIt covers leave."
    chunks = chunk_pages([Page(1, text)], target=500, overlap=50)
    assert [c.text.split("\n")[0] for c in chunks] == ["Introduction", "Scope"]


def test_long_sentence_without_breaks_is_split_by_tokens():
    blob = "x" + " lorem" * 2000  # one "sentence", ~2000 tokens
    chunks = chunk_pages([Page(7, blob)], target=300, overlap=40)
    assert len(chunks) >= 5
    assert all(c.token_count <= 300 + 5 for c in chunks)
    assert all((c.page_from, c.page_to) == (7, 7) for c in chunks)


def test_deterministic():
    pages = [Page(1, words(120, "eps")), Page(2, "SECTION 4 RULES\n" + words(80, "zeta"))]
    assert chunk_pages(pages, target=180, overlap=25) == chunk_pages(pages, target=180, overlap=25)


def test_special_token_text_is_plain_text():
    chunks = chunk_pages([Page(1, "The marker <|endoftext|> appears in this document.")])
    assert "<|endoftext|>" in chunks[0].text


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("1. Purpose", True),
        ("2.3 Tender Evaluation", True),
        ("Section 4 Records", True),
        ("ANNEXURE A", True),
        ("LEAVE POLICY CIRCULAR", True),
        ("The applicant shall submit a declaration.", False),
        ("2024 was a year in which the committee met eleven times and decided many things.", False),
        ("NOTE: SEE ABOVE.", False),
    ],
)
def test_is_heading_line(line, expected):
    assert is_heading_line(line) is expected


def test_overlap_must_be_smaller_than_target():
    with pytest.raises(ValueError):
        chunk_pages([Page(1, "x")], target=50, overlap=50)
