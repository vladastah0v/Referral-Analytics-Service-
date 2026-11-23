import logging
from time import monotonic

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.modules.tracking.metrics import OUTBOX_OLDEST, OUTBOX_PENDING
from app.modules.tracking.models import EventOutbox
from app.modules.tracking.outbox import process_next
from app.workers.celery_app import celery_app
from app.workers.runtime import run_job

logger = logging.getLogger(__name__)


async def drain_outbox(factory, max_seconds: float = 20) -> int:
    processed = 0
    deadline = monotonic() + max_seconds
    while monotonic() < deadline:
        async with factory() as session:
            if not await process_next(session, retry_unexpected=True):
                break
            processed += 1
    return processed


async def drain_and_measure(factory) -> int:
    processed = await drain_outbox(factory, get_settings().outbox_drain_seconds)
    try:
        async with factory() as session:
            pending, oldest = (
                await session.execute(
                    select(
                        func.count(),
                        func.extract(
                            "epoch", func.clock_timestamp() - func.min(EventOutbox.created_at)
                        ),
                    ).where(EventOutbox.state == "pending")
                )
            ).one()
        OUTBOX_PENDING.set(pending)
        OUTBOX_OLDEST.set(max(0, oldest or 0))
    except SQLAlchemyError as exc:
        logger.warning("Could not measure outbox backlog", extra={"error_type": type(exc).__name__})
    return processed


@celery_app.task(bind=True, name="tracking.drain_outbox", acks_late=True, max_retries=5)
def drain_outbox_task(self):
    return run_job(self, drain_and_measure)
