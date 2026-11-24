"""retrieve(): question -> context blocks. answer_events(): blocks -> streamed answer + citations.

retrieve() does every DB read on a short session of its own and closes it before returning, so the
answer stream (seconds of LLM time) holds no pooled MySQL connection. answer_events() works only on
plain data (frozen Block dataclasses).
"""

import time
from collections.abc import Iterator
from dataclasses import dataclass

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import Chunk, Document
from ..search_index import IndexConfig
from .decompose import decompose, needs_decomposition
from .llm import LLM, LLMError, LLMTimeout, Usage
from .prompt import ABSTAIN, Block, answer_messages, extract_citations, is_abstain, title_from_filename
from .query import text_query
from .rerank import Candidate, rerank
from .search import Hit, hybrid_search, merge_hits, vector_search
from .usage import UsageLog

STRATEGIES = ("vector_only", "hybrid", "hybrid_rerank")


class UpstreamError(Exception):
    """The query embedding failed: nothing can be retrieved (502)."""


@dataclass(frozen=True)
class RetrievalConfig:
    strategy: str = "hybrid_rerank"
    decompose: bool = True
    k: int = 20
    rerank_candidates: int = 20
    top_n: int = 6
    passage_tokens: int = 200

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown retrieval strategy {self.strategy!r}; expected one of {STRATEGIES}")

    @classmethod
    def from_settings(cls, s: Settings) -> "RetrievalConfig":
        return cls(
            strategy=s.retrieval_strategy,
            decompose=s.retrieval_decompose,
            k=s.retrieval_k,
            rerank_candidates=s.rerank_candidates,
            top_n=s.context_blocks,
            passage_tokens=s.rerank_passage_tokens,
        )

    @property
    def label(self) -> str:
        return self.strategy + ("+decompose" if self.decompose else "")


@dataclass
class RetrievalDeps:
    settings: Settings
    sessions: sessionmaker[Session]
    redis: redis.Redis
    index: IndexConfig
    embedder: object  # apps.worker.embedding.OpenAIEmbedder | FakeEmbedder (embed_with_usage)
    llm: LLM


@dataclass(frozen=True)
class Retrieved:
    blocks: list[Block]
    sub_queries: list[str]
    n_candidates: int


def _to_bytes(vector: list[float]) -> bytes:
    from apps.worker.embedding import to_float32_bytes

    return to_float32_bytes(vector)


def _search(deps: RetrievalDeps, cfg: RetrievalConfig, tenant_id: int, query: str, vector: bytes) -> list[Hit]:
    if cfg.strategy == "vector_only":
        return vector_search(deps.redis, deps.index, tenant_id, vector, cfg.k)
    text_q = text_query(query)
    if text_q is None:  # nothing searchable in text: the vector half alone
        return vector_search(deps.redis, deps.index, tenant_id, vector, cfg.k)
    return hybrid_search(deps.redis, deps.index, tenant_id, text_q, vector, cfg.k)


def load_candidates(sessions: sessionmaker[Session], tenant_id: int, hits: list[Hit]) -> list[Candidate]:
    """Chunk text (MySQL is the truth) for the ranked hits: same tenant, active version only, order kept."""
    if not hits:
        return []
    with sessions() as session:  # closed on exit: the connection goes back to the pool here
        rows = session.execute(
            select(Chunk.id, Chunk.document_id, Chunk.text, Chunk.page_from, Chunk.page_to, Document.filename)
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.id.in_([h.chunk_id for h in hits]),
                Chunk.tenant_id == tenant_id,
                # The version switch (docs/adr/0004): during a replay Redis can briefly hold keys of two
                # versions; this one consistent read serves exactly the active one, never both.
                Chunk.version == Document.active_version,
            )
        ).all()
    by_id = {r.id: r for r in rows}
    return [
        Candidate(
            chunk_id=h.chunk_id,
            document_id=by_id[h.chunk_id].document_id,
            title=title_from_filename(by_id[h.chunk_id].filename),
            page_from=by_id[h.chunk_id].page_from,
            page_to=by_id[h.chunk_id].page_to,
            text=by_id[h.chunk_id].text,
            score=h.score,
        )
        for h in hits
        if h.chunk_id in by_id  # a stale key (inactive version, or chunk gone) is skipped, never invented
    ]


def retrieve(deps: RetrievalDeps, tenant_id: int, question: str, cfg: RetrievalConfig, usage: UsageLog) -> Retrieved:
    s = deps.settings
    sub_queries = [question]
    if cfg.decompose and needs_decomposition(question):
        sub_queries = decompose(deps.llm, s.openai_fast_model, question, usage, s.fast_reasoning_effort)

    started = time.perf_counter()
    try:
        vectors, tokens = deps.embedder.embed_with_usage(sub_queries)  # one call for every sub-query
    except Exception as exc:
        raise UpstreamError(f"query embedding failed ({type(exc).__name__})") from exc
    usage.record(
        "embed_query", getattr(deps.embedder, "model", "?"), Usage(prompt_tokens=tokens),
        (time.perf_counter() - started) * 1000,
    )

    hits = merge_hits(
        [_search(deps, cfg, tenant_id, q, _to_bytes(v)) for q, v in zip(sub_queries, vectors)],
        cfg.rerank_candidates,
    )
    candidates = load_candidates(deps.sessions, tenant_id, hits)
    if len(candidates) < len(hits):
        usage.note(stale_hits=len(hits) - len(candidates))

    if cfg.strategy == "hybrid_rerank":
        chosen = rerank(
            deps.llm, s.openai_fast_model, question, candidates, cfg.top_n, cfg.passage_tokens, usage,
            s.fast_reasoning_effort,
        )
    else:
        chosen = candidates[: cfg.top_n]

    blocks = [
        Block(n, c.chunk_id, c.document_id, c.title, c.page_from, c.page_to, c.text)
        for n, c in enumerate(chosen, start=1)
    ]
    usage.note(strategy=cfg.label, sub_queries=len(sub_queries), candidates=len(candidates), blocks=len(blocks))
    return Retrieved(blocks=blocks, sub_queries=sub_queries, n_candidates=len(candidates))


Event = tuple[str, dict]  # ("token", {"text"}) | ("citations", {...}) | ("error", {"code", "message"})


def answer_events(deps: RetrievalDeps, question: str, blocks: list[Block], usage: UsageLog) -> Iterator[Event]:
    """Token events, then exactly one citations event, or an error event if the model call fails."""
    if not blocks:  # nothing retrieved: abstain without spending an LLM call
        usage.note(abstained=True, cited=0)
        yield "token", {"text": ABSTAIN}
        yield "citations", {"citations": [], "abstained": True}
        return

    s = deps.settings
    system, user = answer_messages(question, blocks)
    parts: list[str] = []
    started = time.perf_counter()
    try:
        for item in deps.llm.stream(s.openai_chat_model, system, user, s.answer_max_tokens, s.chat_reasoning_effort):
            if isinstance(item, Usage):
                usage.record("answer", s.openai_chat_model, item, (time.perf_counter() - started) * 1000)
            else:
                parts.append(item)
                yield "token", {"text": item}
    except LLMTimeout as exc:
        usage.note(answer_error=exc.code)
        yield "error", {"code": exc.code, "message": "the answer model timed out"}
        return
    except LLMError as exc:
        usage.note(answer_error=exc.code)
        yield "error", {"code": exc.code, "message": "the answer model failed"}
        return

    answer = "".join(parts)
    abstained = is_abstain(answer)
    valid, invalid = ([], []) if abstained else extract_citations(answer, len(blocks))
    usage.note(abstained=abstained, cited=len(valid), invalid_citations=len(invalid), uncited=not valid and not abstained)
    by_n = {b.n: b for b in blocks}
    yield "citations", {"citations": [by_n[n].citation() for n in valid], "abstained": abstained}
