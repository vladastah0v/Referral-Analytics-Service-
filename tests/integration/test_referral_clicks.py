import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid1, uuid4

from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.referrals.models import ReferralClick, ReferralCode
from tests.integration.test_referral_codes import authenticated

TRACK_URL = "/api/v1/referrals/track"
CODES_URL = "/api/v1/referrals/codes"


async def create_code(client: AsyncClient) -> tuple[dict[str, str], dict]:
    headers = await authenticated(client, "referrer@example.com")
    result = await client.post(CODES_URL, headers=headers, json={})
    assert result.status_code == 201
    return headers, result.json()


def click_payload(code: str) -> dict:
    return {
        "code": code,
        "visitor_id": str(uuid4()),
        "request_id": str(uuid4()),
        "source": "social",
        "campaign": "spring",
    }


async def test_click_creation_replay_and_conflict(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    headers, code = await create_code(client)
    payload = click_payload(code["code"])
    first = await client.post(TRACK_URL, json=payload)
    assert first.status_code == 201
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["referral_code_id"] == code["id"]
    assert "visitor_id" not in first.json()
    replay = await client.post(TRACK_URL, json=payload)
    assert replay.status_code == 200 and replay.json() == first.json()
    for changed in (
        {"visitor_id": str(uuid4())},
        {"code": "another"},
        {"source": "other"},
        {"campaign": None},
    ):
        assert (await client.post(TRACK_URL, json=payload | changed)).status_code == 409
    assert (
        await client.patch(f"{CODES_URL}/{code['id']}", headers=headers, json={"is_active": False})
    ).status_code == 200
    replay_after_deactivation = await client.post(TRACK_URL, json=payload)
    assert replay_after_deactivation.status_code == 200
    assert replay_after_deactivation.json() == first.json()
    assert (await client.post(TRACK_URL, json=click_payload(code["code"]))).status_code == 404
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ReferralClick)) == 1


async def test_missing_expired_and_invalid_input(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, code = await create_code(client)
    payload = click_payload(code["code"])
    assert (await client.post(TRACK_URL, json=payload | {"code": "missing"})).status_code == 404
    for changes in (
        {"visitor_id": str(uuid1())},
        {"request_id": str(uuid1())},
        {"visitor_id": "invalid"},
        {"source": "   "},
        {"campaign": "x" * 129},
        {"referrer_id": str(uuid4())},
    ):
        assert (await client.post(TRACK_URL, json=payload | changes)).status_code == 422
    async with session_maker() as session:
        await session.execute(
            update(ReferralCode)
            .where(ReferralCode.id == code["id"])
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    assert (await client.post(TRACK_URL, json=payload)).status_code == 404
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ReferralClick)) == 0


async def test_concurrent_identical_requests_create_one_click(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, code = await create_code(client)
    payload = click_payload(code["code"])
    responses = await asyncio.gather(*(client.post(TRACK_URL, json=payload) for _ in range(8)))
    assert sorted(response.status_code for response in responses) == [200] * 7 + [201]
    assert len({response.json()["id"] for response in responses}) == 1
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ReferralClick)) == 1


async def test_concurrent_conflicting_requests_have_one_winner(client: AsyncClient) -> None:
    headers, code = await create_code(client)
    second_code = (await client.post(CODES_URL, headers=headers, json={})).json()
    payload = click_payload(code["code"])
    other = payload | {"code": second_code["code"]}
    responses = await asyncio.gather(
        client.post(TRACK_URL, json=payload), client.post(TRACK_URL, json=other)
    )
    assert sorted(response.status_code for response in responses) == [201, 409]


async def test_max_uses_does_not_limit_clicks(client: AsyncClient) -> None:
    headers = await authenticated(client, "referrer@example.com")
    code = (await client.post(CODES_URL, headers=headers, json={"max_uses": 1})).json()
    first = click_payload(code["code"])
    second = click_payload(code["code"]) | {"visitor_id": first["visitor_id"]}
    assert (await client.post(TRACK_URL, json=first)).status_code == 201
    assert (await client.post(TRACK_URL, json=second)).status_code == 201
