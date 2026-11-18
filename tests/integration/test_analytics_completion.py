import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import insert, update

from app.modules.analytics import router as routes
from app.modules.analytics.cache import get_cache_client
from app.modules.analytics.conversion import compare_rates
from app.modules.leads.models import Lead
from app.modules.rewards.models import ReferralReward, ReferralRewardHistory
from app.modules.users.models import User, UserRole
from tests.integration.test_lead_creation import own_user_id
from tests.integration.test_overview import seed_overview
from tests.integration.test_referral_codes import authenticated
from tests.integration.test_referral_programs import staff_headers
from tests.integration.test_referral_statistics import stamp
from tests.integration.test_reward_models import reward_data

BASE = "/api/v1/analytics/"


async def test_conversion_periods_and_revenue_filters(client, session_maker):
    customer, code = await seed_overview(client, session_maker)
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    args = {"date_from": stamp(2).isoformat(), "date_to": stamp(3).isoformat(), "group_by": "day"}
    for headers, result in (({}, 401), (customer, 403), (manager, 403)):
        assert (
            await client.get(BASE + "conversion", headers=headers, params=args)
        ).status_code == result
    data = (await client.get(BASE + "conversion", headers=admin, params=args)).json()
    assert data["previous"]["referrals"]["conversion_rate"] == "100.00"
    assert data["current"]["referrals"]["conversion_rate"] == "33.33"
    assert data["referral_change"] == {
        "percentage_points": "-66.67",
        "relative_growth_percent": "-66.67",
    }
    assert data["lead_change"] == {"percentage_points": None, "relative_growth_percent": None}
    assert len(data["current"]["referrals"]["periods"]) == 1
    assert compare_rates(1, 10, 3, 20).relative_growth_percent == Decimal("50.00")
    assert compare_rates(0, 10, 3, 20).percentage_points == Decimal("15.00")
    assert compare_rates(0, 10, 3, 20).relative_growth_percent is None
    explicit = args | {"previous_from": stamp(1).isoformat(), "previous_to": stamp(2).isoformat()}
    assert (await client.get(BASE + "conversion", headers=admin, params=explicit)).json() == data
    for change in (
        {"previous_from": stamp(1).isoformat()},
        {"previous_to": stamp(2).isoformat()},
        {"previous_from": stamp(1).isoformat(), "previous_to": stamp(3).isoformat()},
        {"date_from": args["date_to"]},
        {"date_to": "2026-01-03T00:00:00"},
    ):
        assert (
            await client.get(BASE + "conversion", headers=admin, params=args | change)
        ).status_code == 422
    filters = args | {
        "source": "mail",
        "campaign": "winter",
        "referral_code": code,
        "currency": "KZT",
    }
    revenue = (await client.get(BASE + "revenue", headers=admin, params=filters)).json()
    assert revenue["periods"][0]["items"] == revenue["items"]
    assert revenue["items"][0]["net_revenue"] == "23.01"
    assert (
        await client.get(BASE + "revenue", headers=admin, params=filters | {"campaign": "other"})
    ).json()["items"] == []


async def test_reward_history_changes_across_period_boundary(client, session_maker, db_engine):
    async with db_engine.begin() as con:
        values = await reward_data(con)
        rid = uuid4()
        await con.execute(insert(ReferralReward).values(id=rid, **values))
        for rev, amount, status, day in (
            (1, 100, "pending", 1),
            (2, 100, "approved", 2),
            (3, 60, "pending", 3),
            (4, 0, "cancelled", 4),
        ):
            await con.execute(
                insert(ReferralRewardHistory).values(
                    reward_id=rid,
                    revision=rev,
                    reward_amount=amount,
                    status=status,
                    reason="test",
                    created_at=stamp(day),
                )
            )
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    args = {"group_by": "day", "date_from": stamp(2).isoformat(), "date_to": stamp(4).isoformat()}
    data = (await client.get(BASE + "overview", headers=admin, params=args)).json()
    assert data["rewards"] == [{"currency": "KZT", "total_rewards": "-40.00"}]
    assert [(p["period_start"], p["rewards"]) for p in data["periods"]] == [
        ("2026-01-03T00:00:00Z", data["rewards"])
    ]
    assert (
        await client.get(BASE + "overview", headers=admin, params=args | {"source": "unknown"})
    ).json()["rewards"] == []


async def test_redis_cache_scope_hit_invalidation_and_fallback(
    client, session_maker, test_app, monkeypatch
):
    redis_url = os.getenv("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("Set TEST_REDIS_URL to run the Redis integration test")
    cache = Redis.from_url(redis_url, decode_responses=True)
    test_app.dependency_overrides[get_cache_client] = lambda: cache
    customer, _ = await seed_overview(client, session_maker)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    other = await authenticated(client, "second-manager@example.com")
    other_id = await own_user_id(client, other)
    manager_id = await own_user_id(client, manager)
    async with session_maker() as session:
        await session.execute(update(User).where(User.id == other_id).values(role=UserRole.MANAGER))
        await session.execute(update(Lead).values(assigned_manager_id=manager_id))
        await session.commit()
    calls = 0
    original = routes.funnel_report

    async def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(routes, "funnel_report", counted)

    async def report(headers, **params):
        return (await client.get(BASE + "funnel", headers=headers, params=params)).json()

    try:
        first = await report(admin)
        assert first["total_leads"] == 1 and calls == 1
        assert await report(admin) == first and calls == 1
        assert (await report(admin, source="other"))["total_leads"] == 0 and calls == 2
        assert (await report(manager))["total_leads"] == 1 and calls == 3
        assert (await report(other))["total_leads"] == 0 and calls == 4
        assert (await client.get(BASE + "funnel", headers=customer)).status_code == 403
        async with session_maker() as session:
            await session.execute(update(Lead).values(assigned_manager_id=None))
            await session.commit()
        assert (await report(manager))["total_leads"] == 0 and calls == 5
        assert (await report(manager))["total_leads"] == 0 and calls == 5

        async def unavailable(*args, **kwargs):
            raise RedisConnectionError("unavailable")

        monkeypatch.setattr(cache, "get", unavailable)
        assert (await report(admin))["total_leads"] == 1 and calls == 6
    finally:
        await cache.aclose()


async def test_report_cache_expires_and_recovers_from_bad_entry(
    client, session_maker, test_app, monkeypatch
):
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("Set TEST_REDIS_URL to run the Redis integration test")
    async with Redis.from_url(url, decode_responses=True) as cache:
        test_app.dependency_overrides[get_cache_client] = lambda: cache
        admin = await staff_headers(client, session_maker, UserRole.ADMIN)
        keys = []
        original_set = cache.set

        async def record(key, *args, **kwargs):
            keys.append(key)
            return await original_set(key, *args, **kwargs)

        monkeypatch.setattr(cache, "set", record)
        endpoint = BASE + "overview"
        first = (await client.get(endpoint, headers=admin)).json()
        assert len(keys) == 1 and 0 < await cache.pttl(keys[0]) <= 60000
        await original_set(keys[0], "broken", ex=60)
        assert (await client.get(endpoint, headers=admin)).json() == first
        assert keys == [keys[0], keys[0]]
        await cache.pexpire(keys[0], 5)
        await asyncio.sleep(0.04)
        assert (await client.get(endpoint, headers=admin)).json() == first and len(keys) == 3
        assert (await client.get(endpoint, headers=admin)).json() == first and len(keys) == 3


async def test_report_revision_tracks_visible_commits_and_rollbacks(session_maker, client):
    from sqlalchemy import func, select

    from app.modules.analytics.models import AnalyticsCacheRevision

    customer, _ = await seed_overview(client, session_maker)
    user_id = await own_user_id(client, customer)

    async def version():
        async with session_maker() as reader:
            return await reader.scalar(select(func.sum(AnalyticsCacheRevision.revision)))

    initial = await version()
    async with session_maker() as first, session_maker() as second:
        first.add(Lead(user_id=user_id, source="first"))
        await first.flush()
        assert await version() == initial
        second.add(Lead(user_id=user_id, source="second"))
        await second.commit()
        assert await version() == initial + 1
        await first.commit()
        assert await version() == initial + 2
    async with session_maker() as writer:
        writer.add(Lead(user_id=user_id, source="discarded"))
        await writer.flush()
        await writer.rollback()
        await writer.execute(update(User).where(User.id == user_id).values(full_name="New name"))
        await writer.commit()
    assert await version() == initial + 2
