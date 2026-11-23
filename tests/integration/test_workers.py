from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.modules.analytics.models import DailySnapshot, SnapshotFailure, SnapshotRequest
from app.modules.auth.models import AuthSession
from app.modules.leads.models import Lead
from app.modules.tracking.models import Event, EventOutbox
from app.modules.tracking.outbox import MAX_ATTEMPTS
from app.modules.users.models import User, UserRole
from app.workers.tasks import maintenance
from app.workers.tasks.outbox import drain_outbox
from tests.integration.test_lead_creation import own_user_id
from tests.integration.test_order_events import EVENTS, event, service_key
from tests.integration.test_overview import seed_overview
from tests.integration.test_referral_attribution import click, make_code
from tests.integration.test_referral_programs import staff_headers
from tests.integration.test_referral_statistics import stamp


async def test_fact_ingestion_replay_and_visibility(client, session_maker):
    owner, code = await make_code(client, "owner@example.com")
    clicked = await click(client, code["code"], uuid4())
    user_id = await own_user_id(client, owner)
    lead = (await client.post("/api/v1/leads", headers=owner, json={"source": "web"})).json()
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    mid = await own_user_id(client, manager)
    assert (
        await client.patch(
            f"/api/v1/leads/{lead['id']}/assign", headers=admin, json={"manager_id": str(mid)}
        )
    ).status_code == 200
    for stage in ("contacted", "qualified"):
        updated = await client.patch(
            f"/api/v1/leads/{lead['id']}/status", headers=manager, json={"status": stage}
        )
        assert updated.status_code == 200
    async with session_maker() as session:
        registered = (await session.get(User, user_id)).created_at
    messages = [
        event(
            "lead_qualified",
            {"lead_id": lead["id"]},
            datetime.fromisoformat(updated.json()["qualified_at"]),
        ),
        event("user_registered", {"user_id": str(user_id)}, registered),
        event(
            "referral_clicked",
            {"click_id": clicked["id"]},
            datetime.fromisoformat(clicked["clicked_at"]),
        ),
        event("lead_created", {"lead_id": lead["id"]}, datetime.fromisoformat(lead["created_at"])),
    ]
    key = await service_key(session_maker)
    response = await client.post(EVENTS + "/batch", headers=key, json={"events": messages})
    ids = [r["event"]["id"] for r in response.json()["results"]]
    assert await drain_outbox(session_maker) == 4
    assert await drain_outbox(session_maker) == 0
    for msg, eid in zip(messages, ids, strict=True):
        assert (await client.post(EVENTS, headers=key, json=msg)).status_code == 200
        assert (await client.get(EVENTS + "/" + eid, headers=admin)).json()[
            "processing_status"
        ] == "processed"
        manager_response = await client.get(EVENTS + "/" + eid, headers=manager)
        assert manager_response.status_code == (
            404 if msg["event_type"] == "referral_clicked" else 200
        )
    assert (await client.post(EVENTS, headers=owner, json=messages[0])).status_code == 401
    invalid = event("lead_created", {"lead_id": lead["id"], "referrer_id": str(mid)})
    assert (await client.post(EVENTS, headers=key, json=invalid)).status_code == 422
    mismatch = event(
        "user_registered", {"user_id": str(user_id)}, registered - timedelta(seconds=1)
    )
    receipt = (await client.post(EVENTS, headers=key, json=mismatch)).json()
    assert await drain_outbox(session_maker) == 1
    assert (await client.get(EVENTS + "/" + receipt["id"], headers=admin)).json()[
        "error_code"
    ] == "fact_time_conflict"


async def test_missing_fact_and_poison_event_do_not_block_queue(client, session_maker, monkeypatch):
    from app.modules.tracking import outbox

    uid = uuid4()
    key = await service_key(session_maker)
    msg = event("user_registered", {"user_id": str(uid)})
    response = await client.post(EVENTS, headers=key, json=msg)
    eid = UUID(response.json()["id"])
    assert await drain_outbox(session_maker) == 1
    async with session_maker() as session:
        row = await session.scalar(select(EventOutbox).where(EventOutbox.event_id == eid))
        assert row.attempts == 1 and row.state == "pending" and row.last_error == "fact_missing"
        row.available_at = stamp(1)
        row.attempts = MAX_ATTEMPTS - 1
        await session.commit()

    async def broken(*args):
        raise RuntimeError("sensitive error detail")

    monkeypatch.setattr(outbox, "confirm_fact", broken)
    assert await drain_outbox(session_maker) == 1
    async with session_maker() as session:
        row = await session.scalar(select(EventOutbox).where(EventOutbox.event_id == eid))
        assert row.attempts == MAX_ATTEMPTS and row.state == "failed"
        assert row.last_error == (await session.get(Event, eid)).error_code == "processing_error"


async def test_notification_processed_after_registration_arrives(client, session_maker):
    key = await service_key(session_maker)
    uid = uuid4()
    registered_at = datetime.now(UTC)
    message = event("user_registered", {"user_id": str(uid)}, registered_at)
    receipt = await client.post(EVENTS, headers=key, json=message)
    assert receipt.status_code == 202
    eid = UUID(receipt.json()["id"])
    assert await drain_outbox(session_maker) == 1
    async with session_maker() as session:
        item = await session.scalar(select(EventOutbox).where(EventOutbox.event_id == eid))
        assert item.state == "pending" and item.last_error == "fact_missing"
        session.add(
            User(
                id=uid,
                created_at=registered_at,
                email="registered@example.com",
                full_name="Registered User",
                role=UserRole.CUSTOMER,
                password_hash="unused",
            )
        )
        item.available_at = registered_at
        await session.commit()
    assert await drain_outbox(session_maker) == 1
    async with session_maker() as session:
        saved = await session.get(Event, eid)
        assert saved.user_id == uid and saved.processing_status == "processed"
        item = await session.scalar(select(EventOutbox).where(EventOutbox.event_id == eid))
        assert item.state == "done" and item.attempts == 2 and item.last_error is None


async def test_daily_refresh_replay_and_write_during_snapshot(client, session_maker, monkeypatch):
    await seed_overview(client, session_maker)
    day = date(2026, 1, 1)
    original = maintenance.overview_report

    async def write_after_snapshot(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with session_maker() as writer:
            user_id = await writer.scalar(select(Lead.user_id).limit(1))
            writer.add(Lead(user_id=user_id, created_at=stamp(1), source="late"))
            await writer.commit()
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(maintenance, "overview_report", write_after_snapshot)
        assert await maintenance.refresh_day(session_maker, str(day))
    async with session_maker() as session:
        assert (await session.get(DailySnapshot, day)).report["funnel"]["total_leads"] == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(SnapshotRequest).where(SnapshotRequest.day == day)
            )
            == 1
        )
    assert await maintenance.refresh_day(session_maker, str(day))
    async with session_maker() as session:
        saved = await session.get(DailySnapshot, day)
        assert saved.report["funnel"]["total_leads"] == 2
        refreshed = saved.refreshed_at
    assert await maintenance.refresh_day(session_maker, str(day))
    async with session_maker() as session:
        assert (await session.get(DailySnapshot, day)).refreshed_at == refreshed
        assert (
            await session.scalar(
                select(func.count()).select_from(SnapshotRequest).where(SnapshotRequest.day == day)
            )
            == 0
        )


async def test_cleanup_preserves_active_session(client, session_maker):
    headers, _ = await make_code(client, "session@example.com")
    uid = await own_user_id(client, headers)
    async with session_maker() as session:
        session.add(
            AuthSession(
                user_id=uid,
                refresh_token_hash="f" * 64,
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await session.commit()
    assert await maintenance.cleanup_sessions(session_maker) == 1
    assert await maintenance.cleanup_sessions(session_maker) == 0
    assert await own_user_id(client, headers) == uid


async def test_failed_snapshot_can_be_rebuilt_without_blocking_other_days(
    session_maker, monkeypatch
):
    failed_day = date(2026, 1, 1)
    healthy_day = date(2026, 1, 2)
    async with session_maker() as session:
        session.add_all([SnapshotRequest(day=failed_day), SnapshotRequest(day=healthy_day)])
        await session.commit()
    async with session_maker() as session:
        failed_id = await session.scalar(
            select(SnapshotRequest.id).where(SnapshotRequest.day == failed_day)
        )
    await maintenance.mark_failed(session_maker, failed_day, "failed-task", failed_id, 1)
    published = []
    monkeypatch.setattr(maintenance.refresh_day_task, "delay", lambda *args: published.append(args))
    assert await maintenance.dispatch_pending(session_maker) == 1
    assert [item[0] for item in published] == [healthy_day.isoformat()]
    assert await maintenance.refresh_day(session_maker, failed_day.isoformat())
    async with session_maker() as session:
        assert await session.get(SnapshotFailure, failed_day) is None
        assert await session.get(DailySnapshot, failed_day) is not None
        assert (await session.scalars(select(SnapshotRequest.day))).all() == [healthy_day]


async def test_obsolete_snapshot_failure_cannot_block_new_requests(session_maker, monkeypatch):
    day = date(2026, 1, 1)
    async with session_maker() as session:
        previous = SnapshotRequest(day=day)
        session.add(previous)
        await session.commit()
        previous_id = previous.id
    assert await maintenance.refresh_day(session_maker, day.isoformat())
    async with session_maker() as session:
        session.add(SnapshotRequest(day=day))
        await session.commit()
    await maintenance.mark_failed(session_maker, day, "old-task", previous_id, 1)
    dispatched = []
    monkeypatch.setattr(
        maintenance.refresh_day_task, "delay", lambda *args: dispatched.append(args)
    )
    assert await maintenance.dispatch_pending(session_maker) == 1
    assert dispatched[0][0] == day.isoformat()
    async with session_maker() as session:
        assert await session.get(SnapshotFailure, day) is None


async def test_truncate_enqueues_materialized_days(db_engine, session_maker):
    from sqlalchemy import text

    day = date(2026, 1, 1)
    assert await maintenance.refresh_day(session_maker, day.isoformat())
    async with db_engine.begin() as connection:
        await connection.execute(text("TRUNCATE referral_clicks CASCADE"))
    async with session_maker() as session:
        assert await session.scalar(select(SnapshotRequest.day)) == day
