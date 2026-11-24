"""Celery app: one queue per stage (docs/adr/0003).

    celery -A apps.worker.celery_app worker -Q parse -c 2
"""

import logging

from celery import Celery
from celery.signals import worker_ready

from apps.api.config import Settings
from apps.api.search_index import ensure_index, index_config, make_redis

# The broker visibility timeout bounds how long an unacked task of a SIGKILLed/OOM-killed worker
# waits before redelivery. Every task must finish well inside it, hence the time limits below.
VISIBILITY_TIMEOUT_S = 1800
STAGE_SOFT_TIME_LIMIT_S = 1200
STAGE_TIME_LIMIT_S = 1500

# Applied to every stage task explicitly (CLAUDE.md invariant), not only via app defaults.
STAGE_TASK_OPTIONS = {
    "acks_late": True,
    "reject_on_worker_lost": True,
    "soft_time_limit": STAGE_SOFT_TIME_LIMIT_S,
    "time_limit": STAGE_TIME_LIMIT_S,
    "ignore_result": True,
}

_settings = Settings()

app = Celery(
    "pagecite",
    broker=_settings.celery_broker_url,
    include=[
        "apps.worker.tasks.parse",
        "apps.worker.tasks.chunk",
        "apps.worker.tasks.embed",
        "apps.worker.tasks.index",
    ],
)
app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # a long OCR task must not hold other tasks hostage
    task_ignore_result=True,
    broker_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT_S},
    broker_connection_retry_on_startup=True,
    task_routes={
        "pagecite.parse": {"queue": "parse"},
        "pagecite.chunk": {"queue": "chunk"},
        "pagecite.embed": {"queue": "embed"},
        "pagecite.index": {"queue": "index"},
    },
)


@worker_ready.connect
def _create_index_at_startup(**_kwargs) -> None:
    """'Create the index at startup if it doesn't exist' (P2)."""
    created = ensure_index(make_redis(_settings.redis_url), index_config(_settings))
    logging.getLogger("pagecite.worker").info("index_ready created=%s", created)


# Register the stage tasks in every process that imports the app, not only in workers (`include`
# is loaded at worker start). The relay is a pure producer and enqueues through this registry.
from .tasks import chunk, embed, index, parse  # noqa: E402,F401  (circular by design: tasks import `app`)
