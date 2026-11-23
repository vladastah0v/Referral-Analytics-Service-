import asyncio
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from app.modules.analytics.models import DailySnapshot, SnapshotFailure, SnapshotRequest
from app.modules.analytics.overview import overview_report
from app.modules.auth.models import AuthSession
from app.modules.users.models import User, UserRole
from app.workers.celery_app import celery_app
from app.workers.runtime import run_job, with_database


async def refresh_day(factory, iso_day: str) -> bool:
    day = date.fromisoformat(iso_day)
    start = datetime.combine(day, time.min, UTC)
    async with factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        # A simultaneous refresh either finishes this day or leaves its requests for beat.
        if not await session.scalar(
            select(func.pg_try_advisory_xact_lock(0x444159, day.toordinal()))
        ):
            return False
        # Pure report scope: global aggregates, with no persisted account or credentials.
        report = await overview_report(
            session,
            User(role=UserRole.ADMIN),
            start,
            start + timedelta(days=1),
            None,
            None,
            None,
            None,
        )
        payload = report.model_dump(mode="json")
        create = insert(DailySnapshot).values(day=day, report=payload)
        await session.execute(
            create.on_conflict_do_update(
                index_elements=[DailySnapshot.day],
                set_={"report": create.excluded.report, "refreshed_at": func.clock_timestamp()},
                where=DailySnapshot.report.is_distinct_from(create.excluded.report),
            )
        )
        # Under repeatable read this cannot consume markers committed after the snapshot.
        await session.execute(delete(SnapshotFailure).where(SnapshotFailure.day == day))
        await session.execute(delete(SnapshotRequest).where(SnapshotRequest.day == day))
        await session.commit()
        return True


async def dispatch_pending(factory) -> int:
    pending = (
        select(
            SnapshotRequest.day,
            func.max(SnapshotRequest.id).label("request_id"),
            func.count().label("request_count"),
            func.min(SnapshotRequest.id).label("first_id"),
        )
        .group_by(SnapshotRequest.day)
        .subquery()
    )
    async with factory() as session:
        requests = (
            await session.execute(
                select(pending.c.day, pending.c.request_id, pending.c.request_count)
                .where(
                    ~select(SnapshotFailure.day)
                    .where(
                        SnapshotFailure.day == pending.c.day,
                        SnapshotFailure.request_id == pending.c.request_id,
                        SnapshotFailure.request_count == pending.c.request_count,
                    )
                    .exists()
                )
                .order_by(pending.c.first_id)
                .limit(31)
            )
        ).all()
    for day, request_id, request_count in requests:
        refresh_day_task.delay(day.isoformat(), request_id, request_count)
    return len(requests)


async def dispatch_yesterday(factory) -> str:
    async with factory() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        day = now.astimezone(UTC).date() - timedelta(days=1)
        # Persist the request first; the periodic dispatcher recovers a lost broker message.
        session.add(SnapshotRequest(day=day))
        await session.flush()
        request_id, request_count = (
            await session.execute(
                select(func.max(SnapshotRequest.id), func.count()).where(SnapshotRequest.day == day)
            )
        ).one()
        await session.commit()
    refresh_day_task.delay(day.isoformat(), request_id, request_count)
    return day.isoformat()


async def cleanup_sessions(factory) -> int:
    async with factory() as session:
        expired = (
            select(AuthSession.id)
            .where(AuthSession.expires_at <= func.now())
            .order_by(AuthSession.expires_at, AuthSession.id)
            .limit(1000)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(
            delete(AuthSession)
            .where(AuthSession.id.in_(expired))
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return result.rowcount


async def mark_failed(factory, day, task_id, request_id, request_count):
    async with factory() as session:
        await session.execute(select(func.pg_advisory_xact_lock(0x444159, day.toordinal())))
        current = (
            await session.execute(
                select(func.max(SnapshotRequest.id), func.count()).where(SnapshotRequest.day == day)
            )
        ).one()
        # Another refresh may have finished, or a new request may have committed in the meantime.
        if tuple(current) != (request_id, request_count):
            return
        statement = insert(SnapshotFailure).values(
            day=day, task_id=task_id, request_id=request_id, request_count=request_count
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[SnapshotFailure.day],
                set_={
                    "task_id": task_id,
                    "request_id": request_id,
                    "request_count": request_count,
                    "failed_at": func.clock_timestamp(),
                },
            )
        )
        await session.commit()


@celery_app.task(bind=True, name="analytics.refresh_day", acks_late=True, max_retries=5)
def refresh_day_task(self, iso_day, request_id=None, request_count=None):
    day = date.fromisoformat(iso_day)
    try:
        return run_job(self, refresh_day, iso_day)
    except RuntimeError:
        if self.request.retries >= self.max_retries and request_id is not None:
            asyncio.run(with_database(mark_failed, day, self.request.id, request_id, request_count))
        raise


@celery_app.task(bind=True, name="analytics.recalculate_pending", acks_late=True, max_retries=5)
def recalculate_pending_task(self):
    return run_job(self, dispatch_pending)


@celery_app.task(bind=True, name="analytics.daily_snapshot", acks_late=True, max_retries=5)
def daily_snapshot_task(self):
    return run_job(self, dispatch_yesterday)


@celery_app.task(bind=True, name="maintenance.cleanup_sessions", acks_late=True, max_retries=5)
def cleanup_sessions_task(self):
    return run_job(self, cleanup_sessions)
