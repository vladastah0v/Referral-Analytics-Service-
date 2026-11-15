import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.referrals.models import (
    AttributionModel,
    ReferralAttribution,
    ReferralClick,
    ReferralCode,
)
from app.modules.users.models import User
from tests.integration.test_referral_codes import authenticated

URL = "/api/v1/referrals/statistics"
CODES = "/api/v1/referrals/codes"


def stamp(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, tzinfo=UTC)


async def seed_statistics(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> tuple[dict[str, str], dict[str, str], UUID]:
    owner = await authenticated(client, "owner@example.com")
    other = await authenticated(client, "other@example.com")
    first = (await client.post(CODES, headers=owner, json={})).json()
    second = (await client.post(CODES, headers=owner, json={})).json()
    other_code = (await client.post(CODES, headers=other, json={})).json()
    owner_id = UUID(first["owner_id"])
    other_id = UUID(other_code["owner_id"])
    first_id, second_id, other_code_id = (UUID(code["id"]) for code in (first, second, other_code))
    visitor_a, visitor_b, visitor_c, visitor_other = (uuid4() for _ in range(4))
    click_ids = [uuid4() for _ in range(6)]
    users = [uuid4() for _ in range(3)]
    clicks = [
        (first_id, visitor_a, stamp(1)),
        (second_id, visitor_a, stamp(2)),
        (first_id, visitor_b, stamp(2, 12)),
        (second_id, visitor_b, stamp(3)),
        (first_id, visitor_c, stamp(4)),
        (other_code_id, visitor_other, stamp(2)),
    ]
    async with session_maker() as session:
        await session.execute(
            insert(User),
            [
                {
                    "id": user_id,
                    "email": f"{user_id}@example.com",
                    "password_hash": "hash",
                    "full_name": "Test",
                    "role": "customer",
                }
                for user_id in users
            ],
        )
        await session.execute(
            insert(ReferralClick),
            [
                {
                    "id": click_ids[index],
                    "referral_code_id": code_id,
                    "visitor_id": visitor_id,
                    "request_id": uuid4(),
                    "clicked_at": clicked_at,
                }
                for index, (code_id, visitor_id, clicked_at) in enumerate(clicks)
            ],
        )
        await session.execute(
            insert(ReferralAttribution),
            [
                {
                    "id": uuid4(),
                    "referral_code_id": first_id,
                    "referrer_id": owner_id,
                    "referred_user_id": users[0],
                    "click_id": click_ids[0],
                    "attribution_model": AttributionModel.LAST_REFERRAL_CLICK,
                    "attributed_at": stamp(1, 12),
                },
                {
                    "id": uuid4(),
                    "referral_code_id": second_id,
                    "referrer_id": owner_id,
                    "referred_user_id": users[1],
                    "click_id": click_ids[1],
                    "attribution_model": AttributionModel.LAST_REFERRAL_CLICK,
                    "attributed_at": stamp(3),
                },
                {
                    "id": uuid4(),
                    "referral_code_id": other_code_id,
                    "referrer_id": other_id,
                    "referred_user_id": users[2],
                    "click_id": click_ids[5],
                    "attribution_model": AttributionModel.LAST_REFERRAL_CLICK,
                    "attributed_at": stamp(2, 1),
                },
            ],
        )
        await session.execute(
            update(ReferralCode).where(ReferralCode.id == second_id).values(is_active=False)
        )
        await session.commit()
    return owner, other, other_id


async def test_statistics_counts_distinct_visitors_and_protects_other_users(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    owner, other, other_id = await seed_statistics(client, session_maker)
    assert (await client.get(URL)).status_code == 401
    first, second = await asyncio.gather(
        client.get(URL, headers=owner, params={"owner_id": str(other_id)}),
        client.get(URL, headers=other),
    )
    assert first.status_code == second.status_code == 200
    assert first.headers["cache-control"] == "private, no-store"
    assert first.json() == {
        "date_from": None,
        "date_to": None,
        "total_clicks": 5,
        "unique_visitors": 3,
        "referred_registrations": 2,
        "conversion_rate": "66.67",
    }
    assert second.json() == {
        "date_from": None,
        "date_to": None,
        "total_clicks": 1,
        "unique_visitors": 1,
        "referred_registrations": 1,
        "conversion_rate": "100.00",
    }


async def test_period_uses_utc_half_open_ranges_and_each_events_timestamp(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    owner, _, _ = await seed_statistics(client, session_maker)
    response = await client.get(
        URL,
        headers=owner,
        params={
            "date_from": "2026-01-02T05:00:00+05:00",
            "date_to": "2026-01-04T05:00:00+05:00",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data == {
        "date_from": "2026-01-02T00:00:00Z",
        "date_to": "2026-01-04T00:00:00Z",
        "total_clicks": 3,
        "unique_visitors": 2,
        "referred_registrations": 1,
        "conversion_rate": "50.00",
    }
    no_clicks = await client.get(
        URL,
        headers=owner,
        params={"date_from": "2026-01-01T10:00:00Z", "date_to": "2026-01-01T13:00:00Z"},
    )
    assert no_clicks.json()["total_clicks"] == 0
    assert no_clicks.json()["referred_registrations"] == 1
    assert no_clicks.json()["conversion_rate"] is None
    until_day_two = (
        await client.get(URL, headers=owner, params={"date_to": "2026-01-02T00:00:00Z"})
    ).json()
    assert until_day_two["total_clicks"] == 1 and until_day_two["referred_registrations"] == 1
    since_day_four = (
        await client.get(URL, headers=owner, params={"date_from": "2026-01-04T00:00:00Z"})
    ).json()
    assert since_day_four["total_clicks"] == 1 and since_day_four["referred_registrations"] == 0


async def test_empty_stats_and_invalid_ranges(client: AsyncClient) -> None:
    owner = await authenticated(client, "empty@example.com")
    response = await client.get(URL, headers=owner)
    assert response.status_code == 200
    assert response.json() == {
        "date_from": None,
        "date_to": None,
        "total_clicks": 0,
        "unique_visitors": 0,
        "referred_registrations": 0,
        "conversion_rate": None,
    }
    for params in (
        {"date_from": "2026-01-02T00:00:00Z", "date_to": "2026-01-02T00:00:00Z"},
        {"date_from": "2026-01-03T00:00:00Z", "date_to": "2026-01-02T00:00:00Z"},
        {"date_from": "2026-01-01T00:00:00"},
        {"date_to": "not-a-date"},
    ):
        assert (await client.get(URL, headers=owner, params=params)).status_code == 422


async def test_conversion_rounding_and_shared_visitor(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    owner = await authenticated(client, "rounding@example.com")
    code = (await client.post(CODES, headers=owner, json={})).json()
    code_id, owner_id = UUID(code["id"]), UUID(code["owner_id"])
    click_ids = [uuid4() for _ in range(32)]
    user_ids = [uuid4() for _ in range(33)]
    async with session_maker() as session:
        await session.execute(
            insert(User),
            [
                {
                    "id": user_id,
                    "email": f"{user_id}@example.com",
                    "password_hash": "hash",
                    "full_name": "Test",
                    "role": "customer",
                }
                for user_id in user_ids
            ],
        )
        await session.execute(
            insert(ReferralClick),
            [
                {
                    "id": click_id,
                    "referral_code_id": code_id,
                    "visitor_id": uuid4(),
                    "request_id": uuid4(),
                    "clicked_at": stamp(1),
                }
                for click_id in click_ids
            ],
        )
        rows = [
            {
                "id": uuid4(),
                "referral_code_id": code_id,
                "referrer_id": owner_id,
                "referred_user_id": user_id,
                "click_id": click_ids[0],
                "attribution_model": AttributionModel.LAST_REFERRAL_CLICK,
                "attributed_at": stamp(2),
            }
            for user_id in user_ids
        ]
        await session.execute(insert(ReferralAttribution), rows[:1])
        await session.commit()
    first = (await client.get(URL, headers=owner)).json()
    assert first["unique_visitors"] == 32 and first["conversion_rate"] == "3.13"
    async with session_maker() as session:
        await session.execute(insert(ReferralAttribution), rows[1:])
        await session.commit()
    second = (await client.get(URL, headers=owner)).json()
    assert second["referred_registrations"] == 33 and second["conversion_rate"] == "103.13"
