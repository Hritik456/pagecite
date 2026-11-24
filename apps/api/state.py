"""Per-app dependencies (settings, DB sessions, storage) and per-request accessors."""

import threading
from dataclasses import dataclass, field
from typing import Any

from flask import current_app, g
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .quotas import TenantLimits
from .storage import Storage

EXTENSION_KEY = "pagecite"


@dataclass
class AppState:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    storage: Storage
    retrieval: Any = None  # RetrievalDeps, built on first /ask (or injected by tests)
    _retrieval_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def get_retrieval():
    """Redis client, embedder and LLM for /ask, built lazily once per app (thread-safe)."""
    state = app_state()
    if state.retrieval is None:
        with state._retrieval_lock:
            if state.retrieval is None:
                state.retrieval = build_retrieval(state)
    return state.retrieval


def build_retrieval(state: "AppState"):
    from apps.worker.embedding import OpenAIEmbedder, UnconfiguredEmbedder

    from .retrieval.llm import OpenAILLM, UnconfiguredLLM
    from .retrieval.pipeline import RetrievalDeps
    from .search_index import index_config, make_redis

    s = state.settings
    key = s.openai_api_key.get_secret_value() if s.openai_api_key else ""
    return RetrievalDeps(
        settings=s,
        sessions=state.sessions,  # the app's pool: retrieval checks out and returns a connection
        redis=make_redis(s.redis_url),
        index=index_config(s),
        embedder=(
            OpenAIEmbedder(key, s.openai_embed_model, s.embed_dim, timeout_s=s.embed_timeout_s, max_retries=1)
            if key else UnconfiguredEmbedder()
        ),
        llm=OpenAILLM(key, json_timeout_s=s.json_timeout_s, answer_timeout_s=s.answer_timeout_s) if key else UnconfiguredLLM(),
    )


@dataclass(frozen=True)
class TenantContext:
    id: int
    limits: TenantLimits


def app_state() -> AppState:
    return current_app.extensions[EXTENSION_KEY]


def get_session() -> Session:
    """One session per request; closed in teardown."""
    if "db_session" not in g:
        g.db_session = app_state().sessions()
    return g.db_session


def close_session(_exc: BaseException | None = None) -> None:
    session = g.pop("db_session", None)
    if session is not None:
        session.close()


def get_storage() -> Storage:
    return app_state().storage


def current_tenant() -> TenantContext:
    return g.tenant
