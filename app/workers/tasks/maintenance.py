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
    async with factory() as session:
        days = list(
            await session.scalars(
                select(SnapshotRequest.day)
                .where(
                    ~select(SnapshotFailure.day)
                    .where(SnapshotFailure.day == SnapshotRequest.day)
                    .exists()
                )
                .group_by(SnapshotRequest.day)
                .order_by(func.min(SnapshotRequest.id))
                .limit(31)
            )
        )
    for day in days:
        refresh_day_task.delay(day.isoformat())
    return len(days)


async def dispatch_yesterday(factory) -> str:
    async with factory() as session:
        now = await session.scalar(select(func.clock_timestamp()))
    day = (now.astimezone(UTC).date() - timedelta(days=1)).isoformat()
    # Persist the request first; the periodic dispatcher recovers a lost broker message.
    async with factory() as session:
        session.add(SnapshotRequest(day=date.fromisoformat(day)))
        await session.commit()
    refresh_day_task.delay(day)
    return day


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


async def mark_failed(factory, day, task_id):
    async with factory() as session:
        statement = insert(SnapshotFailure).values(day=day, task_id=task_id)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[SnapshotFailure.day],
                set_={"task_id": task_id, "failed_at": func.clock_timestamp()},
            )
        )
        await session.commit()


@celery_app.task(bind=True, name="analytics.refresh_day", acks_late=True, max_retries=5)
def refresh_day_task(self, iso_day):
    # Validate the operator argument before applying transient database retries.
    day = date.fromisoformat(iso_day)
    try:
        return run_job(self, refresh_day, iso_day)
    except RuntimeError:
        if self.request.retries >= self.max_retries:
            asyncio.run(with_database(mark_failed, day, self.request.id))
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
