from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.processor import DeferredOrderEvent, OrderEventProcessor, RejectedOrderEvent
from app.modules.tracking.facts import confirm_fact
from app.modules.tracking.models import Event, EventOutbox

MAX_ATTEMPTS = 10


async def process_next(session: AsyncSession, *, retry_unexpected: bool = False) -> bool:
    row = await session.scalar(
        select(EventOutbox)
        .where(EventOutbox.state == "pending", EventOutbox.available_at <= func.now())
        .order_by(EventOutbox.available_at, EventOutbox.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        await session.rollback()
        return False
    event = await session.get(Event, row.event_id)
    if event is None:
        raise RuntimeError("Outbox event missing")
    try:
        async with session.begin_nested():
            if event.event_type.startswith("order_"):
                await OrderEventProcessor(session).process(event)
            else:
                await confirm_fact(session, event)
    except RejectedOrderEvent as exc:
        row.attempts += 1
        row.state = "failed"
        row.last_error = exc.code
        event.processing_status = "rejected"
        event.error_code = exc.code
    except Exception as exc:
        if not isinstance(exc, DeferredOrderEvent) and not retry_unexpected:
            raise
        row.attempts += 1
        row.last_error = exc.code if isinstance(exc, DeferredOrderEvent) else "processing_error"
        if row.attempts >= MAX_ATTEMPTS:
            row.state = "failed"
            event.processing_status = "rejected"
            event.error_code = (
                "prerequisite_timeout"
                if isinstance(exc, DeferredOrderEvent)
                else "processing_error"
            )
        else:
            now = await session.scalar(select(func.clock_timestamp()))
            row.available_at = now + timedelta(seconds=min(2**row.attempts, 300))
    else:
        row.attempts += 1
        row.state = "done"
        row.last_error = None
        event.processing_status = "processed"
        event.error_code = None
    await session.commit()
    return True


async def requeue_failed_event(session: AsyncSession, event_id: UUID) -> bool:
    item = await session.scalar(
        select(EventOutbox).where(EventOutbox.event_id == event_id).with_for_update()
    )
    if item is None or item.state != "failed":
        return False
    event = await session.get(Event, item.event_id)
    if event is None:
        raise RuntimeError("Outbox event missing")
    item.state = "pending"
    item.attempts = 0
    item.last_error = None
    item.available_at = await session.scalar(select(func.clock_timestamp()))
    event.processing_status = "pending"
    event.error_code = None
    await session.commit()
    return True
