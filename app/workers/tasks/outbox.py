import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.modules.tracking.outbox import process_next
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def drain_outbox() -> int:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    count = 0
    try:
        for _ in range(50):
            async with factory() as session:
                if not await process_next(session):
                    break
                count += 1
    finally:
        await engine.dispose()
    return count


@celery_app.task(bind=True, name="tracking.drain_outbox", acks_late=True, max_retries=5)
def drain_outbox_task(self):
    try:
        return asyncio.run(drain_outbox())
    except Exception:
        logger.error("Outbox task failed task_id=%s", self.request.id)
        raise self.retry(
            exc=RuntimeError("Outbox task failed"), countdown=min(2**self.request.retries, 60)
        ) from None
