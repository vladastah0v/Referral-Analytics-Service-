from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.modules.orders.models import Order, OrderFinancialEvent
from app.modules.rewards.models import ReferralRewardHistory
from app.modules.tracking.models import Event
from app.modules.users.models import UserRole
from app.workers.tasks.outbox import drain_outbox
from scripts.generate_events import order_events, send_events, verify_events
from scripts.seed import seed_database
from tests.integration.test_order_events import service_key
from tests.integration.test_referral_programs import staff_headers
from tests.integration.test_seed import SCENARIO


async def test_financial_scenario_api_outbox_report_and_replay(db_engine, session_maker, client):
    await seed_database(db_engine, SCENARIO)
    seed = SCENARIO.seed
    key = (await service_key(session_maker, f"synthetic-{seed}"))["X-Integration-Key"]
    result = await send_events(session_maker, client, key, seed, 7)
    assert result["accepted"] > result["orders"] == SCENARIO.orders
    assert result["replayed"] == 0
    async with session_maker() as session:
        with pytest.raises(ValueError, match="pending"):
            await verify_events(session, seed)
    while await drain_outbox(session_maker):
        pass
    async with session_maker() as session:
        verified = await verify_events(session, seed)
        states = set(await session.scalars(select(Order.status).distinct()))
        assert states == {"paid", "partially_refunded", "refunded"}
        history_count = await session.scalar(
            select(func.count()).select_from(ReferralRewardHistory)
        )
        assert verified["reward_amount"] > 0 and verified["refunds"] > 0

    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    response = await client.get("/api/v1/analytics/revenue", headers=admin)
    assert response.status_code == 200
    report = response.json()["items"][0]
    for name in ("gross_revenue", "refunds", "net_revenue", "referral_attributed_revenue"):
        assert Decimal(report[name]) == verified[name]

    assert await send_events(session_maker, client, key, seed, 13) == {
        "orders": SCENARIO.orders,
        "accepted": 0,
        "replayed": result["accepted"],
    }
    assert await drain_outbox(session_maker) == 0
    async with session_maker() as session:
        assert await verify_events(session, seed) == verified
        assert (
            await session.scalar(select(func.count()).select_from(OrderFinancialEvent))
            == result["accepted"]
        )
        assert (
            await session.scalar(select(func.count()).select_from(ReferralRewardHistory))
            == history_count
        )


async def test_wrong_source_key_stops_before_first_batch(db_engine, session_maker, client):
    await seed_database(db_engine, SCENARIO)
    other = (await service_key(session_maker, "shop"))["X-Integration-Key"]
    with pytest.raises(ValueError, match="Нужен ключ источника"):
        await send_events(session_maker, client, other, SCENARIO.seed, 100)
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 0


async def test_conflicting_event_in_accepted_batch_is_reported(db_engine, session_maker, client):
    await seed_database(db_engine, SCENARIO)
    seed = SCENARIO.seed
    headers = await service_key(session_maker, f"synthetic-{seed}")
    async with session_maker() as session:
        order = await session.scalar(select(Order).order_by(Order.id).limit(1))
    message = next(order_events(order))
    message["payload"]["amount"] = "1.00"
    assert (await client.post("/api/v1/events", headers=headers, json=message)).status_code == 202
    with pytest.raises(ValueError, match="HTTP 409"):
        await send_events(session_maker, client, headers["X-Integration-Key"], seed, 10)
