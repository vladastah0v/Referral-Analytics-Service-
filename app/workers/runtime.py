import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.metrics import instrument_database

logger = logging.getLogger(__name__)


async def with_database(operation, *args):
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    instrument_database(engine.sync_engine)
    try:
        return await operation(async_sessionmaker(engine, expire_on_commit=False), *args)
    finally:
        await engine.dispose()


def run_job(task, operation, *args):
    """Create and dispose the async engine on the same loop in the worker process."""
    try:
        result = asyncio.run(with_database(operation, *args))
    except Exception:
        logger.error(
            "Background task failed task=%s task_id=%s retry=%s",
            task.name,
            task.request.id,
            task.request.retries,
        )
        raise task.retry(
            exc=RuntimeError("Background task failed"), countdown=min(2**task.request.retries, 60)
        ) from None
    logger.info("Background task completed task=%s task_id=%s", task.name, task.request.id)
    return result
