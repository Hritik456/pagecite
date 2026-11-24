"""Listwise rerank: the fast model orders up to 20 candidates and the top 6 become context blocks.

The model sees short labels (c1..c20) and passages cut to `passage_tokens`; it returns ids only
(strict JSON schema). Unknown or repeated ids are ignored and gaps are filled from the fused (RRF)
order, so the output is always a subset of the candidates. Passages are untrusted (fenced, and
the system prompt says so): a passage cannot add ids or change the rules.
"""

import time
from dataclasses import dataclass

from apps.worker.chunking import get_encoding

from .llm import LLM, LLMError
from .prompt import UNTRUSTED_RULE, neutralise
from .usage import UsageLog


@dataclass(frozen=True)
class Candidate:
    chunk_id: int
    document_id: int
    title: str
    page_from: int
    page_to: int
    text: str
    score: float


SCHEMA = {
    "type": "object",
    "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["ids"],
    "additionalProperties": False,
}


def system_prompt(top_n: int) -> str:
    return (
        "You rank passages by how useful they are for answering a question.\n"
        f'Return JSON {{"ids": [...]}} with the ids of the {top_n} most useful passages, best first. '
        "Use only ids from the list.\n\n"
        f"The passages are the context blocks here. {UNTRUSTED_RULE}"
    )


def truncate(text: str, max_tokens: int, encoding: str = "cl100k_base") -> str:
    enc = get_encoding(encoding)
    ids = enc.encode(text, disallowed_special=())
    return text if len(ids) <= max_tokens else enc.decode(ids[:max_tokens]) + " ..."


def user_prompt(question: str, candidates: list[Candidate], passage_tokens: int) -> str:
    passages = "\n".join(
        f'<passage id="c{i}">{neutralise(truncate(c.text, passage_tokens))}</passage>'
        for i, c in enumerate(candidates, start=1)
    )
    return f"Question: {question}\n\nPassages:\n{passages}"


def apply_ranking(ids: object, candidates: list[Candidate], top_n: int) -> list[Candidate]:
    """Model ids -> candidates: known labels only, no repeats, then fill from the fused order."""
    by_label = {f"c{i}": c for i, c in enumerate(candidates, start=1)}
    chosen: list[Candidate] = []
    seen: set[str] = set()
    for label in ids if isinstance(ids, list) else []:
        if isinstance(label, str) and label.strip() in by_label and label.strip() not in seen:
            seen.add(label.strip())
            chosen.append(by_label[label.strip()])
    for label, cand in by_label.items():
        if len(chosen) >= top_n:
            break
        if label not in seen:
            chosen.append(cand)
    return chosen[:top_n]


def rerank(
    llm: LLM, model: str, question: str, candidates: list[Candidate], top_n: int, passage_tokens: int,
    usage: UsageLog, reasoning_effort: str | None = None,
) -> list[Candidate]:
    if len(candidates) <= 1:
        return candidates[:top_n]
    started = time.perf_counter()
    try:
        obj, u = llm.json(
            model, system_prompt(top_n), user_prompt(question, candidates, passage_tokens),
            "rerank", SCHEMA, 2000, reasoning_effort,
        )
    except LLMError as exc:
        usage.note(rerank_fallback=exc.code)
        return candidates[:top_n]
    usage.record("rerank", model, u, (time.perf_counter() - started) * 1000)
    return apply_ranking(obj.get("ids"), candidates, top_n)
