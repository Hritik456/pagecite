"""Per-process worker resources, created lazily (after Celery forks its pool). Tests swap them in."""

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytesseract
import redis
from PIL.Image import Image
from sqlalchemy.orm import Session, sessionmaker

from apps.api.config import Settings
from apps.api.db import make_engine
from apps.api.search_index import IndexConfig, index_config, make_redis
from apps.api.storage import S3Storage, Storage

from .embedding import Embedder, OpenAIEmbedder, UnconfiguredEmbedder
from .errors import PermanentError
from .parsing import OcrFn, OcrLimits

Enqueue = Callable[..., None]  # enqueue(task_name, *args)


def tesseract_ocr(image: Image, timeout_s: int) -> str:
    try:
        return pytesseract.image_to_string(image, lang="eng", timeout=timeout_s)
    except RuntimeError as exc:  # pytesseract signals its timeout this way
        if "timeout" in str(exc).lower():
            raise PermanentError("OCR_PAGE_TIMEOUT", f"OCR of one page exceeded {timeout_s} s") from exc
        raise


def celery_enqueue(task_name: str, *args) -> None:
    from .celery_app import app  # late import: the Celery app imports the task modules

    # apply_async (not send_task) so eager mode in tests runs the chain in-process.
    app.tasks[task_name].apply_async(args=args)


@dataclass
class WorkerContext:
    settings: Settings
    sessions: sessionmaker[Session]
    storage: Storage
    redis: redis.Redis
    index: IndexConfig
    embedder: Embedder
    ocr: OcrFn
    enqueue: Enqueue = celery_enqueue
    clock: Callable[[], float] = time.monotonic
    index_ready: bool = field(default=False)

    @property
    def ocr_limits(self) -> OcrLimits:
        s = self.settings
        return OcrLimits(max_pages=s.ocr_max_pages, page_timeout_s=s.ocr_page_timeout_s, budget_s=s.ocr_budget_s)


def build_context(settings: Settings | None = None) -> WorkerContext:
    settings = settings or Settings()
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
    embedder: Embedder = (
        OpenAIEmbedder(key, settings.openai_embed_model, settings.embed_dim) if key else UnconfiguredEmbedder()
    )
    return WorkerContext(
        settings=settings,
        sessions=sessionmaker(make_engine(settings.database_url), expire_on_commit=False),
        storage=S3Storage(settings),
        redis=make_redis(settings.redis_url),
        index=index_config(settings),
        embedder=embedder,
        ocr=tesseract_ocr,
    )


_context: WorkerContext | None = None
_context_pid: int | None = None
_pinned = False


def get_context() -> WorkerContext:
    """One context per process: a forked pool child builds its own (DB pools must not cross a fork)."""
    global _context, _context_pid
    if _pinned and _context is not None:
        return _context
    if _context is None or _context_pid != os.getpid():
        _context = build_context()
        _context_pid = os.getpid()
    return _context


@contextmanager
def use_context(ctx: WorkerContext) -> Iterator[WorkerContext]:
    """Tests: run tasks against the given context."""
    global _context, _pinned
    previous, previous_pinned = _context, _pinned
    _context, _pinned = ctx, True
    try:
        yield ctx
    finally:
        _context, _pinned = previous, previous_pinned
