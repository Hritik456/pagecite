"""Lexical overlap between a golden question and its source quote(s): the BM25-bias guard. Pure.

A question drafted from page text can copy the quote's wording, which favours the text (BM25) half of
hybrid retrieval for reasons that have nothing to do with real users. The score is

    |content words of the question that also appear in the quotes| / |content words of the question|

where "content words" use exactly the tokeniser and stopword list of the text query
(apps/api/retrieval/query.py), so it measures what BM25 actually sees.

For `identifier` and `bank_type` rows the identifier's parts and the bank-type words are removed first:
those rows must contain them (that is what they test), and only the paraphrased rest is scored.
"""

from apps.api.retrieval.query import _terms, identifier_phrases

FLAG_ABOVE = 0.6

# Words that name a bank type in the twin circulars. Removed only for `bank_type` rows.
BANK_TYPE_WORDS = frozenset(
    """
    bank banks banking commercial scheduled small finance sfb sfbs payments payment pb pbs regional rural rrb rrbs
    urban co operative cooperative ucb ucbs state central district sts dccb dccbs stcb stcbs local area lab labs
    primary all india financial institutions aifi aifis nbfc nbfcs non companies
    """.split()
)


def _identifier_words(question: str) -> set[str]:
    return {w for phrase in identifier_phrases(question) for w in phrase.split()}


def scored_terms(question: str, kind: str) -> list[str]:
    terms = _terms(question)
    if kind in ("identifier", "bank_type"):
        drop = _identifier_words(question)
        if kind == "bank_type":
            drop |= BANK_TYPE_WORDS
        terms = [t for t in terms if t not in drop]
    return terms


def overlap(question: str, quotes: list[str], kind: str) -> float | None:
    """Share of the question's scored content words found in the quotes; None if there are no quotes."""
    if not quotes:
        return None
    terms = scored_terms(question, kind)
    if not terms:
        return 0.0
    quote_terms = set(_terms(" ".join(quotes)))
    return sum(t in quote_terms for t in terms) / len(terms)


def flagged(score: float | None) -> bool:
    return score is not None and score > FLAG_ABOVE
