import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, update

from app.modules.orders.models import Order, OrderFinancialEvent
from app.modules.orders.processor import OrderEventProcessor
from app.modules.referrals.models import ReferralProgram
from app.modules.rewards.models import ReferralReward, ReferralRewardHistory
from app.modules.rewards.service import RewardService
from app.modules.tracking.models import Event
from tests.integration.test_reward_models import reward_data


async def setup_order(engine, **program_changes):
    async with engine.begin() as connection:
        values = await reward_data(connection)
        now = datetime.now(UTC)
        await connection.execute(
            update(Order)
            .where(Order.id == values["order_id"])
            .values(status="created", paid_at=None, external_created_at=now - timedelta(seconds=1))
        )
        if program_changes:
            await connection.execute(
                update(ReferralProgram)
                .where(ReferralProgram.id == values["referral_program_id"])
                .values(**program_changes)
            )
    return values, now


async def process(factory, values, now, kind="order_paid", amount="1000.00", op="payment", **extra):
    async with factory() as session:
        event = Event(
            source_system="shop",
            external_event_id=str(uuid4()),
            event_type=kind,
            occurred_at=now,
            payload_hash="0" * 64,
            payload={
                "external_order_id": str(values["order_id"]),
                "currency": "KZT",
                "operation_id": op,
                "amount": amount,
                **extra,
            },
        )
        session.add(event)
        await OrderEventProcessor(session).process(event)
        await session.commit()


async def state(factory):
    async with factory() as session:
        reward = await session.scalar(select(ReferralReward))
        history = list(
            (
                await session.scalars(
                    select(ReferralRewardHistory).order_by(ReferralRewardHistory.revision)
                )
            ).all()
        )
        return reward, history


async def test_accrual_refunds_snapshot_and_concurrent_duplicate(db_engine, session_maker):
    values, now = await setup_order(db_engine)
    await asyncio.gather(*(process(session_maker, values, now) for _ in range(2)))
    reward, history = await state(session_maker)
    assert reward.reward_amount == Decimal("100.00") and len(history) == 1
    async with session_maker() as session:
        await session.execute(
            update(ReferralProgram).values(
                reward_value=Decimal("50"), currency="USD", is_active=False
            )
        )
        await session.execute(
            update(ReferralReward).values(status="approved", approved_at=datetime.now(UTC))
        )
        await session.commit()
    refund_time = now + timedelta(seconds=1)
    await asyncio.gather(
        *(
            process(session_maker, values, refund_time, "order_refunded", "250.00", "refund1")
            for _ in range(2)
        )
    )
    reward, history = await state(session_maker)
    assert reward.reward_amount == Decimal("75.00")
    assert reward.status == "pending" and reward.approved_at is None
    assert reward.reward_value_snapshot == Decimal("10.00") and len(history) == 2
    await process(
        session_maker, values, now + timedelta(seconds=2), "order_refunded", "750.00", "refund2"
    )
    reward, history = await state(session_maker)
    assert reward.reward_amount == 0 and reward.status == "cancelled"
    assert [row.reward_amount for row in history] == [Decimal("100"), Decimal("75"), Decimal(0)]
    assert len({row.financial_event_id for row in history}) == 3
    await process(session_maker, values, now)  # old payment replay after full refund
    assert len((await state(session_maker))[1]) == 3


async def test_fixed_cap_and_adjustment_recovery(db_engine, session_maker):
    values, now = await setup_order(
        db_engine, reward_type="fixed", reward_value=Decimal("150"), max_reward=Decimal("100")
    )
    await process(session_maker, values, now)
    await process(
        session_maker, values, now + timedelta(seconds=1), "order_refunded", "500.00", "refund"
    )
    assert (await state(session_maker))[0].reward_amount == Decimal("50")
    await process(
        session_maker,
        values,
        now + timedelta(seconds=2),
        "order_adjusted",
        "500.00",
        "debit",
        direction="debit",
    )
    assert (await state(session_maker))[0].status == "cancelled"
    await process(
        session_maker,
        values,
        now + timedelta(seconds=3),
        "order_adjusted",
        "2000.00",
        "credit",
        direction="credit",
    )
    reward, history = await state(session_maker)
    assert reward.reward_amount == Decimal("100") and reward.status == "pending"
    assert len(history) == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"is_active": False},
        {"currency": "USD"},
        {"starts_at": datetime.now(UTC) + timedelta(days=1)},
        {
            "starts_at": datetime.now(UTC) - timedelta(days=2),
            "ends_at": datetime.now(UTC) - timedelta(days=1),
        },
    ],
)
async def test_ineligible_program_does_not_accrue_later(db_engine, session_maker, changes):
    values, now = await setup_order(db_engine, **changes)
    await process(session_maker, values, now)
    assert (await state(session_maker))[0] is None
    async with session_maker() as session:
        await session.execute(
            update(ReferralProgram).values(
                is_active=True, currency="KZT", starts_at=now - timedelta(days=1), ends_at=None
            )
        )
        await session.commit()
    await process(session_maker, values, now)
    await process(
        session_maker, values, now + timedelta(seconds=1), "order_refunded", "100.00", "refund"
    )
    assert (await state(session_maker))[0] is None


async def test_latest_program_and_no_attribution(db_engine, session_maker):
    values, now = await setup_order(db_engine)
    async with session_maker() as session:
        await session.execute(
            insert(ReferralProgram).values(
                name="Older",
                reward_type="fixed",
                reward_value=Decimal("999"),
                currency="KZT",
                starts_at=now - timedelta(days=1),
            )
        )
        await session.commit()
    await process(session_maker, values, now)
    assert (await state(session_maker))[0].referral_program_id == values["referral_program_id"]
    other, later = await setup_order(db_engine)
    async with session_maker() as session:
        await session.execute(
            update(Order).where(Order.id == other["order_id"]).values(referral_attribution_id=None)
        )
        await session.commit()
    await process(session_maker, other, later, op="other-payment")
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ReferralReward)) == 1


async def test_reward_failure_rolls_back_payment_and_history(db_engine, session_maker, monkeypatch):
    values, now = await setup_order(db_engine)
    original = RewardService.reconcile

    async def fail(self, order, operation):
        await original(self, order, operation)
        raise RuntimeError("after reward flush")

    with monkeypatch.context() as scoped:
        scoped.setattr(RewardService, "reconcile", fail)
        with pytest.raises(RuntimeError):
            await process(session_maker, values, now)
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(OrderFinancialEvent)) == 0
        assert await session.scalar(select(func.count()).select_from(ReferralRewardHistory)) == 0
        assert await session.scalar(select(func.count()).select_from(ReferralReward)) == 0
        assert (await session.get(Order, values["order_id"])).status == "created"
    await process(session_maker, values, now)
    assert len((await state(session_maker))[1]) == 1


async def test_late_adjustments_and_paid_status(db_engine, session_maker):
    values, now = await setup_order(db_engine, max_reward=Decimal("100"))
    await process(session_maker, values, now)
    async with session_maker() as session:
        await session.execute(
            update(ReferralReward).values(status="paid", approved_at=datetime.now(UTC))
        )
        await session.commit()
    await process(
        session_maker,
        values,
        now + timedelta(seconds=2),
        "order_adjusted",
        "100.00",
        "credit",
        direction="credit",
    )
    reward, history = await state(session_maker)
    assert reward.status == "paid" and len(history) == 1  # unchanged capped amount
    await process(
        session_maker, values, now + timedelta(seconds=1), "order_refunded", "500.00", "late-refund"
    )
    reward, history = await state(session_maker)
    assert reward.reward_amount == Decimal("60")
    assert reward.status == "pending" and reward.approved_at is None and len(history) == 2
