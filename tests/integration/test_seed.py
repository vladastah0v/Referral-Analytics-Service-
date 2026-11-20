import asyncio
from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from app.modules.leads.models import Lead, LeadStatusHistory
from app.modules.orders.models import Order
from app.modules.referrals.models import ReferralAttribution, ReferralClick, ReferralCode
from app.modules.users.models import User
from scripts.seed import Scenario, build_rows, seed_database

SCENARIO = Scenario(
    seed=15,
    start=date(2025, 1, 1),
    days=30,
    users=100,
    clicks=120,
    referrals=25,
    leads=50,
    orders=40,
)


async def test_concurrent_seed_replay_and_chronology(db_engine, session_maker):
    results = await asyncio.gather(
        seed_database(db_engine, SCENARIO), seed_database(db_engine, SCENARIO)
    )
    assert sorted(results) == [False, True]
    expected = dict(build_rows(SCENARIO))
    assert expected == dict(build_rows(SCENARIO))
    async with session_maker() as session:
        for model, rows in expected.items():
            assert set(await session.scalars(select(model.id))) == {row["id"] for row in rows}
        users = {user.id: user for user in await session.scalars(select(User))}
        codes = {code.id: code for code in await session.scalars(select(ReferralCode))}
        clicks = {click.id: click for click in await session.scalars(select(ReferralClick))}
        attrs = {}
        for attr in await session.scalars(select(ReferralAttribution)):
            click = clicks[attr.click_id]
            code = codes[attr.referral_code_id]
            user = users[attr.referred_user_id]
            assert (
                click.referral_code_id == code.id and code.owner_id == attr.referrer_id != user.id
            )
            assert code.created_at <= click.clicked_at < user.created_at == attr.attributed_at
            assert user.created_at - click.clicked_at <= timedelta(days=30)
            attrs[user.id] = attr.id
        for model in (Lead, Order):
            for row in await session.scalars(select(model)):
                assert row.created_at > users[row.user_id].created_at
                assert row.referral_attribution_id == attrs.get(row.user_id)
        for change in await session.scalars(select(LeadStatusHistory)):
            if change.previous_status:
                assert users[change.changed_by].role == "manager"
    with pytest.raises(ValueError, match="другими параметрами"):
        await seed_database(db_engine, replace(SCENARIO, days=31))


async def test_seed_collision_rolls_back_earlier_batches(db_engine):
    code = dict(build_rows(SCENARIO))[ReferralCode][0]["code"]
    async with db_engine.begin() as conn:
        owner_id = await conn.scalar(
            insert(User)
            .values(
                email="existing@example.com",
                full_name="Existing",
                password_hash="!",
                role="customer",
            )
            .returning(User.id)
        )
        await conn.execute(insert(ReferralCode).values(owner_id=owner_id, code=code))
    with pytest.raises(IntegrityError):
        await seed_database(db_engine, SCENARIO)
    async with db_engine.connect() as conn:
        assert (await conn.scalars(select(User.full_name))).all() == ["Existing"]
        assert (await conn.scalars(select(ReferralCode.owner_id))).all() == [owner_id]
        assert await conn.scalar(select(func.count()).select_from(ReferralClick)) == 0
