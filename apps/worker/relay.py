"""Outbox relay: unpublished outbox_events -> Celery `parse` tasks.

    python -m apps.worker.relay

Each pass locks a batch of unpublished rows with FOR UPDATE SKIP LOCKED (several relays can run
side by side without double-publishing), enqueues them, then marks them published in the same
transaction. If enqueueing fails the transaction rolls back and the rows are retried: delivery is
at-least-once, which the idempotent stage tasks absorb.
"""

import logging
import signal
import time

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from apps.api.models import OutboxEvent, StageName
from apps.api.request_log import configure_logging

from .context import Enqueue, get_context
from .stages import TASK_NAMES

log = logging.getLogger("pagecite.relay")

DOCUMENT_UPLOADED = "document.uploaded"


def relay_once(session: Session, enqueue: Enqueue, limit: int = 100) -> int:
    """Publish up to `limit` events. Returns how many rows were published."""
    with session.begin():
        events = session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        if not events:
            return 0
        for event in events:
            if event.type == DOCUMENT_UPLOADED:
                enqueue(TASK_NAMES[StageName.PARSE], event.payload["document_id"], event.payload["version"])
            else:
                log.warning("outbox_unknown_type id=%s type=%s", event.id, event.type)
        session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_([e.id for e in events]))
            .values(published_at=func.now(6))
            .execution_options(synchronize_session=False)
        )
    log.info("outbox_published count=%s first_id=%s last_id=%s", len(events), events[0].id, events[-1].id)
    return len(events)


def main(poll_interval_s: float = 1.0) -> None:
    ctx = get_context()
    configure_logging(ctx.settings.log_level)
    stopping = False

    def stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("relay_started poll_interval_s=%s", poll_interval_s)
    while not stopping:
        try:
            with ctx.sessions() as session:
                published = relay_once(session, ctx.enqueue)
        except Exception as exc:  # DB or broker down: keep the loop alive and try again
            log.error("relay_error type=%s", type(exc).__name__, exc_info=True)
            published = 0
        if not published:
            time.sleep(poll_interval_s)
    log.info("relay_stopped")


if __name__ == "__main__":
    main()
