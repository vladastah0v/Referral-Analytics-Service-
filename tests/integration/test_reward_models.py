from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.orders.models import Order
from app.modules.referrals.models import (
    ReferralAttribution,
    ReferralClick,
    ReferralCode,
    ReferralProgram,
)
from app.modules.rewards.models import ReferralReward, ReferralRewardHistory
from tests.integration.test_lead_models import add_user, rejected


async def reward_data(connection):
    referrer, customer = await add_user(connection), await add_user(connection)
    code, click, attribution, order, program = [uuid4() for _ in range(5)]
    await connection.execute(
        insert(ReferralCode).values(id=code, owner_id=referrer, code=str(code))
    )
    await connection.execute(
        insert(ReferralClick).values(
            id=click, referral_code_id=code, visitor_id=uuid4(), request_id=uuid4()
        )
    )
    await connection.execute(
        insert(ReferralAttribution).values(
            id=attribution,
            referral_code_id=code,
            referrer_id=referrer,
            referred_user_id=customer,
            click_id=click,
            attribution_model="last_referral_click",
        )
    )
    await connection.execute(
        insert(Order).values(
            id=order,
            user_id=customer,
            source_system="shop",
            external_order_id=str(order),
            referral_attribution_id=attribution,
            attribution_frozen_at=datetime.now(UTC),
            status="paid",
            paid_at=datetime.now(UTC),
            total_amount=Decimal("1000.00"),
            currency="KZT",
        )
    )
    await connection.execute(
        insert(ReferralProgram).values(
            id=program,
            name="Program",
            reward_type="percentage",
            reward_value=Decimal("10.00"),
            currency="KZT",
            starts_at=datetime.now(UTC),
        )
    )
    return dict(
        referral_program_id=program,
        referral_attribution_id=attribution,
        order_id=order,
        referrer_id=referrer,
        reward_amount=Decimal("100.00"),
        currency="KZT",
        reward_type_snapshot="percentage",
        reward_value_snapshot=Decimal("10.00"),
        max_reward_snapshot=Decimal("200.00"),
    )


async def test_reward_snapshot_and_single_reward_per_order(db_engine: AsyncEngine):
    async with db_engine.begin() as connection:
        values = await reward_data(connection)
        reward_id = uuid4()
        await connection.execute(insert(ReferralReward).values(id=reward_id, **values))
        await rejected(connection, insert(ReferralReward).values(id=uuid4(), **values))
        await connection.execute(
            update(ReferralProgram)
            .where(ReferralProgram.id == values["referral_program_id"])
            .values(reward_value=Decimal("20.00"), currency="USD", max_reward=Decimal("300.00"))
        )
        row = (await connection.execute(select(ReferralReward))).one()
        assert row.reward_amount == Decimal("100.00")
        assert isinstance(row.reward_amount, Decimal)
        assert row.reward_value_snapshot == Decimal("10.00")
        assert row.max_reward_snapshot == Decimal("200.00")
        assert row.currency == "KZT" and row.status == "pending"
        assert row.rounding_mode == "ROUND_HALF_UP" and row.approved_at is None
        assert row.created_at.tzinfo is not None
        await rejected(
            connection,
            delete(ReferralProgram).where(ReferralProgram.id == values["referral_program_id"]),
        )
        await rejected(connection, delete(Order).where(Order.id == values["order_id"]))


async def test_reward_rejects_inconsistent_links(db_engine: AsyncEngine):
    async with db_engine.begin() as connection:
        values, other = await reward_data(connection), await reward_data(connection)
        for changes in (
            {"referrer_id": other["referrer_id"]},
            {"referral_attribution_id": other["referral_attribution_id"]},
            {"order_id": other["order_id"]},
            {"currency": "USD"},
            {"referral_program_id": uuid4()},
        ):
            await rejected(
                connection, insert(ReferralReward).values(id=uuid4(), **(values | changes))
            )


async def test_reward_money_rules_and_status_constraints(db_engine: AsyncEngine):
    async with db_engine.begin() as connection:
        values = await reward_data(connection)
        for changes in (
            {"reward_amount": Decimal("-1")},
            {"reward_amount": Decimal("NaN")},
            {"reward_amount": Decimal("201")},
            {"reward_value_snapshot": Decimal("0")},
            {"reward_value_snapshot": Decimal("NaN")},
            {"reward_value_snapshot": Decimal("100.01")},
            {"max_reward_snapshot": Decimal("-1")},
            {"max_reward_snapshot": Decimal("NaN")},
            {"status": "approved"},
            {"status": "paid"},
            {"rounding_mode": "ROUND_DOWN"},
            {"approved_at": datetime.now(UTC) - timedelta(days=1)},
        ):
            await rejected(
                connection, insert(ReferralReward).values(id=uuid4(), **(values | changes))
            )
        await connection.execute(
            insert(ReferralReward).values(
                id=uuid4(),
                **(
                    values
                    | {"reward_type_snapshot": "fixed", "reward_value_snapshot": Decimal("150")}
                ),
            )
        )


async def test_reward_history_keeps_revisions_and_restricts_deletion(db_engine: AsyncEngine):
    async with db_engine.begin() as connection:
        values = await reward_data(connection)
        reward_id = uuid4()
        await connection.execute(insert(ReferralReward).values(id=reward_id, **values))
        first = dict(
            reward_id=reward_id,
            revision=1,
            reward_amount=Decimal("100"),
            status="pending",
            reason="accrual",
        )
        await connection.execute(insert(ReferralRewardHistory).values(id=uuid4(), **first))
        for changes in (
            {},
            {"revision": 0},
            {"revision": 2, "reason": " "},
            {"revision": 2, "reward_amount": Decimal("NaN")},
            {"revision": 2, "actor_id": uuid4()},
        ):
            await rejected(
                connection, insert(ReferralRewardHistory).values(id=uuid4(), **(first | changes))
            )
        await connection.execute(
            insert(ReferralRewardHistory).values(
                id=uuid4(),
                **(
                    first
                    | {
                        "revision": 2,
                        "reward_amount": Decimal("0"),
                        "status": "cancelled",
                        "reason": "refund",
                    }
                ),
            )
        )
        await connection.execute(
            update(ReferralReward)
            .where(ReferralReward.id == reward_id)
            .values(reward_amount=Decimal("0"), status="cancelled")
        )
        history = (
            await connection.execute(
                select(ReferralRewardHistory).order_by(ReferralRewardHistory.revision)
            )
        ).all()
        assert [row.reward_amount for row in history] == [Decimal("100.00"), Decimal("0.00")]
        await rejected(connection, delete(ReferralReward).where(ReferralReward.id == reward_id))
