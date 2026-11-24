import pytest

from apps.api.retrieval.query import identifier_phrases, text_query


def test_terms_are_ored_and_stopwords_dropped():
    assert text_query("What is the daily allowance for Category A cities?") == (
        "daily | allowance | category | cities"
    )


def test_circular_number_becomes_an_exact_phrase_first():
    q = text_query("What does circular FIAS/HR/2025-26/014 say?")
    assert q.startswith('"fias hr 2025 26 014" | ')
    assert "circular" in q.split(" | ")


@pytest.mark.parametrize(
    ("question", "phrases"),
    [
        ("Under clause 4.2(b) of FIAS/HR/2025-26/041", ["4 2 b", "fias hr 2025 26 041"]),
        ("Section 7.3 and section 7.3 again", ["7 3"]),  # de-duplicated
        ("RBI/2024-25/117", ["rbi 2024 25 117"]),
        ("a well-known rule", []),  # hyphenated words without digits are not identifiers
    ],
)
def test_identifier_phrases(question, phrases):
    assert identifier_phrases(question) == phrases


def test_only_safe_characters_reach_redis():
    q = text_query('ignore @tenant:{9} -x | "y" * ~z %w% $v ) (')
    assert set(q) <= set('abcdefghijklmnopqrstuvwxyz0123456789 |"')
    assert "tenant" in q and "9" in q  # words survive, syntax does not


def test_nothing_searchable_returns_none():
    assert text_query("what is it?") is None
    assert text_query("   ") is None


def test_single_digits_kept_single_letters_dropped():
    assert text_query("table 5 row b") == "table | 5 | row"
