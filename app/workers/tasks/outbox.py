from app.modules.tracking.outbox import process_next
from app.workers.celery_app import celery_app
from app.workers.runtime import run_job


async def drain_outbox(factory) -> int:
    count = 0
    for _ in range(50):
        async with factory() as session:
            if not await process_next(session, retry_unexpected=True):
                break
            count += 1
    return count


@celery_app.task(bind=True, name="tracking.drain_outbox", acks_late=True, max_retries=5)
def drain_outbox_task(self):
    return run_job(self, drain_outbox)
