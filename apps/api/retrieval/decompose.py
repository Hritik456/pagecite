"""Query decomposition: one cheap JSON call splits a multi-part question into at most 3 sub-queries.

Skipped for short single-intent questions (P4: fewer than 12 words and no and/compare/vs).
Any failure falls back to the original question: decomposition can only help, never block an answer.
"""

import re
import time

from .llm import LLM, LLMError
from .usage import UsageLog

MAX_SUB_QUERIES = 3
_MULTI_INTENT = re.compile(r"\b(and|compare|compared|comparing|vs\.?|versus)\b", re.IGNORECASE)

SYSTEM = f"""You turn a user's question into search queries for a document search engine.
Return JSON {{"sub_queries": [...]}} with at most {MAX_SUB_QUERIES} standalone queries, one per distinct information need.
If the question has a single need, return it as the only query.
Copy identifiers such as circular numbers, clause numbers, names and dates exactly.
Do not answer the question."""

SCHEMA = {
    "type": "object",
    "properties": {"sub_queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["sub_queries"],
    "additionalProperties": False,
}


def needs_decomposition(question: str) -> bool:
    return not (len(question.split()) < 12 and not _MULTI_INTENT.search(question))


def clean_sub_queries(raw: object, question: str) -> list[str]:
    if not isinstance(raw, list):
        return [question]
    seen: dict[str, str] = {}
    for item in raw:
        if isinstance(item, str) and item.strip():
            text = " ".join(item.split())[:300]
            seen.setdefault(text.lower(), text)
    return list(seen.values())[:MAX_SUB_QUERIES] or [question]


def decompose(
    llm: LLM, model: str, question: str, usage: UsageLog, reasoning_effort: str | None = None
) -> list[str]:
    started = time.perf_counter()
    try:
        obj, u = llm.json(model, SYSTEM, question, "decomposition", SCHEMA, 1200, reasoning_effort)
    except LLMError as exc:
        usage.note(decompose_fallback=exc.code)
        return [question]
    usage.record("decompose", model, u, (time.perf_counter() - started) * 1000)
    return clean_sub_queries(obj.get("sub_queries"), question)
