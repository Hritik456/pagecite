"""Per-app dependencies (settings, DB sessions, storage) and per-request accessors."""

from dataclasses import dataclass

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
