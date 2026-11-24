"""The text half of FT.HYBRID: a question -> a RediSearch query string. Pure.

RediSearch ANDs bare terms, so passing a natural-language question would require every word to
appear in one chunk. Instead the question is tokenised the way the indexer tokenises text
(alphanumeric runs, punctuation as separators), stopwords are dropped, and the terms are OR'd.
Identifiers such as circular numbers (FIAS/HR/2025-26/014) or clause numbers (4.2(b)) are also
added as exact phrases of their parts: "fias hr 2025 26 014". BM25 then rewards the chunk that
contains that exact sequence, which is what vector similarity cannot do (014 and 041 embed alike).
"""

import re

MAX_TERMS = 40

_WORD = re.compile(r"[A-Za-z0-9]+")
# A run of alphanumerics joined by separators that contains at least one digit: 2025-26/014, 4.2(b).
_IDENTIFIER = re.compile(r"[A-Za-z0-9]+(?:[/\-.()]+[A-Za-z0-9]+)+\)?")

STOPWORDS = frozenset(
    """
    a about above after again all am an and any are as at be been before being below between both but by
    can could did do does doing down during each few for from further had has have having he her here hers
    him his how i if in into is it its itself just me more most my no nor not now of off on once only or
    other our ours out over own same she should so some such than that the their theirs them then there
    these they this those through to too under until up very was we were what when where which while who
    whom why will with would you your yours
    """.split()
)


def _terms(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for word in _WORD.findall(text.lower()):
        if word in STOPWORDS or (len(word) == 1 and not word.isdigit()):
            continue
        seen.setdefault(word, None)
    return list(seen)


def identifier_phrases(question: str) -> list[str]:
    phrases: dict[str, None] = {}
    for match in _IDENTIFIER.finditer(question):
        ident = match.group(0)
        parts = _WORD.findall(ident.lower())
        if len(parts) >= 2 and any(p.isdigit() for p in parts):
            phrases.setdefault(" ".join(parts), None)
    return list(phrases)


def text_query(question: str) -> str | None:
    """OR'd terms plus quoted identifier phrases, or None if nothing searchable is left.

    Only [a-z0-9] and spaces ever reach the query, so no RediSearch syntax can be injected.
    """
    phrases = [f'"{p}"' for p in identifier_phrases(question)]
    terms = _terms(question)[:MAX_TERMS]
    parts = phrases + terms
    return " | ".join(parts) if parts else None
