import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid1, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.referrals.models import (
    AttributionModel,
    ReferralAttribution,
    ReferralClick,
    ReferralCode,
)
from app.modules.referrals.service import ReferralAttributionService
from app.modules.users.models import User
from tests.integration.test_auth import PASSWORD
from tests.integration.test_referral_codes import authenticated

CODES = "/api/v1/referrals/codes"
TRACK = "/api/v1/referrals/track"
REGISTER = "/api/v1/auth/register"


async def make_code(
    client: AsyncClient, email: str, max_uses: int | None = None
) -> tuple[dict, dict]:
    headers = await authenticated(client, email)
    response = await client.post(CODES, headers=headers, json={"max_uses": max_uses})
    assert response.status_code == 201
    return headers, response.json()


async def click(client: AsyncClient, code: str, visitor_id: UUID) -> dict:
    response = await client.post(
        TRACK, json={"code": code, "visitor_id": str(visitor_id), "request_id": str(uuid4())}
    )
    assert response.status_code == 201
    return response.json()


async def register_with_visitor(client: AsyncClient, email: str, visitor_id: UUID | None):
    payload = {"email": email, "password": PASSWORD, "full_name": "New User"}
    if visitor_id is not None:
        payload["visitor_id"] = str(visitor_id)
    return await client.post(REGISTER, json=payload)


async def attribution_for(session_maker: async_sessionmaker[AsyncSession], user_id: str):
    async with session_maker() as session:
        return await session.scalar(
            select(ReferralAttribution).where(ReferralAttribution.referred_user_id == UUID(user_id))
        )


async def test_registration_last_click_and_duplicate_registration(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, first_code = await make_code(client, "first@example.com")
    _, second_code = await make_code(client, "second@example.com")
    visitor_id = uuid4()
    await click(client, first_code["code"], visitor_id)
    latest = await click(client, second_code["code"], visitor_id)
    result = await register_with_visitor(client, "new@example.com", visitor_id)
    assert result.status_code == 201
    assert "visitor_id" not in result.json()
    attribution = await attribution_for(session_maker, result.json()["id"])
    assert attribution is not None
    assert str(attribution.click_id) == latest["id"]
    assert str(attribution.referrer_id) == second_code["owner_id"]
    assert attribution.attribution_model == AttributionModel.LAST_REFERRAL_CLICK
    assert (await register_with_visitor(client, "new@example.com", visitor_id)).status_code == 409
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ReferralAttribution)) == 1


async def test_invalid_or_missing_visitor_registers_without_attribution(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    missing = await register_with_visitor(client, "missing@example.com", None)
    unknown = await register_with_visitor(client, "unknown@example.com", uuid4())
    assert missing.status_code == unknown.status_code == 201
    invalid = await register_with_visitor(client, "invalid@example.com", uuid1())
    assert invalid.status_code == 422
    forged = await client.post(
        REGISTER,
        json={
            "email": "forged@example.com",
            "password": PASSWORD,
            "full_name": "User",
            "visitor_id": str(uuid4()),
            "referrer_id": str(uuid4()),
        },
    )
    assert forged.status_code == 422
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ReferralAttribution)) == 0


async def test_fallback_skips_inactive_expired_and_exhausted_codes(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    headers, valid = await make_code(client, "owner@example.com")
    codes = []
    for options in ({}, {}, {"max_uses": 1}):
        response = await client.post(CODES, headers=headers, json=options)
        assert response.status_code == 201
        codes.append(response.json())
    inactive, expired, full = codes
    fill_visitor = uuid4()
    await click(client, full["code"], fill_visitor)
    assert (
        await register_with_visitor(client, "filled@example.com", fill_visitor)
    ).status_code == 201
    visitor_id = uuid4()
    valid_click = await click(client, valid["code"], visitor_id)
    await click(client, inactive["code"], visitor_id)
    await click(client, expired["code"], visitor_id)
    await click(client, full["code"], visitor_id)
    assert (
        await client.patch(f"{CODES}/{inactive['id']}", headers=headers, json={"is_active": False})
    ).status_code == 200
    async with session_maker() as session:
        await session.execute(
            update(ReferralCode)
            .where(ReferralCode.id == UUID(expired["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    result = await register_with_visitor(client, "fallback@example.com", visitor_id)
    assert result.status_code == 201
    attribution = await attribution_for(session_maker, result.json()["id"])
    assert attribution is not None and str(attribution.click_id) == valid_click["id"]


async def test_equal_timestamps_break_ties_by_click_uuid(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    headers, first = await make_code(client, "owner@example.com")
    second = (await client.post(CODES, headers=headers, json={})).json()
    visitor = uuid4()
    click_a = await click(client, first["code"], visitor)
    click_b = await click(client, second["code"], visitor)
    same_time = datetime.now(UTC) - timedelta(days=1)
    async with session_maker() as session:
        await session.execute(
            update(ReferralClick)
            .where(ReferralClick.id.in_([UUID(click_a["id"]), UUID(click_b["id"])]))
            .values(clicked_at=same_time)
        )
        await session.commit()
    result = await register_with_visitor(client, "tie@example.com", visitor)
    attribution = await attribution_for(session_maker, result.json()["id"])
    assert attribution is not None
    assert attribution.click_id == max(UUID(click_a["id"]), UUID(click_b["id"]))


@pytest.mark.parametrize("offset, expected", [(-30, True), (-31, False), (1, False)])
async def test_attribution_window_boundaries(
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    offset: int,
    expected: bool,
) -> None:
    _, code = await make_code(client, "owner@example.com")
    visitor = uuid4()
    tracked = await click(client, code["code"], visitor)
    user_response = await register_with_visitor(client, "window@example.com", None)
    user_id = UUID(user_response.json()["id"])
    async with session_maker() as session:
        user = await session.get(User, user_id)
        # Use an exact 30-day boundary and a future click beyond registration time.
        timestamp = user.created_at + (
            timedelta(microseconds=1) if offset == 1 else timedelta(days=offset)
        )
        await session.execute(
            update(ReferralClick)
            .where(ReferralClick.id == UUID(tracked["id"]))
            .values(clicked_at=timestamp)
        )
        await session.commit()
    async with session_maker() as session:
        result = await ReferralAttributionService(session).attribute_registration(user_id, visitor)
        await session.commit()
        assert (result is not None) is expected


async def test_concurrent_max_uses_and_registration_replay(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, code = await make_code(client, "owner@example.com", max_uses=1)
    visitors = [uuid4() for _ in range(4)]
    await asyncio.gather(*(click(client, code["code"], visitor) for visitor in visitors))
    results = await asyncio.gather(
        *(
            register_with_visitor(client, f"new{index}@example.com", visitor)
            for index, visitor in enumerate(visitors)
        )
    )
    assert all(result.status_code == 201 for result in results)
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ReferralAttribution)) == 1
    responses = await asyncio.gather(
        *(register_with_visitor(client, "new0@example.com", visitors[0]) for _ in range(3))
    )
    assert [response.status_code for response in responses] == [409, 409, 409]
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ReferralAttribution)) == 1


async def test_reprocessing_same_user_is_idempotent_under_concurrency(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    headers, first = await make_code(client, "owner@example.com")
    second = (await client.post(CODES, headers=headers, json={})).json()
    visitor_a, visitor_b = uuid4(), uuid4()
    await click(client, first["code"], visitor_a)
    await click(client, second["code"], visitor_b)
    result = await register_with_visitor(client, "repeat@example.com", None)
    user_id = UUID(result.json()["id"])

    async def process(visitor):
        async with session_maker() as session:
            attribution = await ReferralAttributionService(session).attribute_registration(
                user_id, visitor
            )
            await session.commit()
            return attribution.id

    first_id, second_id = await asyncio.gather(process(visitor_a), process(visitor_b))
    assert first_id == second_id
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ReferralAttribution)) == 1


async def test_failure_rolls_back_user_and_attribution(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    from app.modules.users import service

    _, code = await make_code(client, "owner@example.com")
    visitor = uuid4()
    await click(client, code["code"], visitor)
    original = service.ReferralAttributionService.attribute_registration

    async def fail(self, user_id, visitor_id):
        assert await original(self, user_id, visitor_id) is not None
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(service.ReferralAttributionService, "attribute_registration", fail)
    with pytest.raises(RuntimeError, match="simulated failure"):
        await register_with_visitor(client, "rollback@example.com", visitor)
    async with session_maker() as session:
        assert (
            await session.scalar(select(User).where(User.email == "rollback@example.com")) is None
        )
        assert await session.scalar(select(func.count()).select_from(ReferralAttribution)) == 0


async def test_self_referral_is_not_created(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, code = await make_code(client, "owner@example.com")
    visitor = uuid4()
    tracked = await click(client, code["code"], visitor)
    owner_id = UUID(code["owner_id"])
    async with session_maker() as session:
        owner = await session.get(User, owner_id)
        await session.execute(
            update(ReferralClick)
            .where(ReferralClick.id == UUID(tracked["id"]))
            .values(clicked_at=owner.created_at - timedelta(seconds=1))
        )
        await session.commit()
    async with session_maker() as session:
        result = await ReferralAttributionService(session).attribute_registration(owner_id, visitor)
        await session.commit()
        assert result is None
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ReferralAttribution)) == 0
    assert (await register_with_visitor(client, "owner@example.com", visitor)).status_code == 409


async def test_concurrent_fallback_from_full_codes_does_not_deadlock(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    headers, first = await make_code(client, "owner@example.com", max_uses=1)
    second_response = await client.post(CODES, headers=headers, json={"max_uses": 1})
    second = second_response.json()
    for index, code in enumerate((first, second)):
        visitor = uuid4()
        await click(client, code["code"], visitor)
        assert (
            await register_with_visitor(client, f"filled{index}@example.com", visitor)
        ).status_code == 201
    visitor_a, visitor_b = uuid4(), uuid4()
    await click(client, first["code"], visitor_a)
    await click(client, second["code"], visitor_a)
    await click(client, second["code"], visitor_b)
    await click(client, first["code"], visitor_b)
    results = await asyncio.wait_for(
        asyncio.gather(
            register_with_visitor(client, "parallel_a@example.com", visitor_a),
            register_with_visitor(client, "parallel_b@example.com", visitor_b),
        ),
        timeout=15,
    )
    assert [result.status_code for result in results] == [201, 201]
    for result in results:
        assert await attribution_for(session_maker, result.json()["id"]) is None
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ReferralAttribution)) == 2


async def test_click_timestamp_is_recorded_after_visitor_lock(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    from app.modules.referrals.repository import ReferralClickRepository

    _, code = await make_code(client, "owner@example.com")
    visitor = uuid4()
    lock_attempted = asyncio.Event()
    original = ReferralClickRepository.lock_visitor

    async def observed_lock(self, visitor_id):
        lock_attempted.set()
        await original(self, visitor_id)

    async with session_maker() as blocker:
        await ReferralClickRepository(blocker).lock_visitor(visitor)
        monkeypatch.setattr(ReferralClickRepository, "lock_visitor", observed_lock)
        task = asyncio.create_task(click(client, code["code"], visitor))
        try:
            await asyncio.wait_for(lock_attempted.wait(), timeout=5)
            marker = await blocker.scalar(select(func.clock_timestamp()))
        finally:
            await blocker.commit()
        result = await asyncio.wait_for(task, timeout=5)
    assert datetime.fromisoformat(result["clicked_at"].replace("Z", "+00:00")) >= marker


async def test_fallback_continues_past_first_candidate_page(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from sqlalchemy import insert

    _, owner_code = await make_code(client, "owner@example.com")
    owner_id = UUID(owner_code["owner_id"])
    visitor = uuid4()
    base = datetime.now(UTC) - timedelta(hours=1)
    code_ids = [uuid4() for _ in range(101)]
    click_ids = [uuid4() for _ in range(101)]
    async with session_maker() as session:
        await session.execute(
            insert(ReferralCode),
            [
                {
                    "id": code_id,
                    "owner_id": owner_id,
                    "code": f"bulk-{code_id}",
                    "is_active": index == 0,
                }
                for index, code_id in enumerate(code_ids)
            ],
        )
        await session.execute(
            insert(ReferralClick),
            [
                {
                    "id": click_ids[index],
                    "referral_code_id": code_id,
                    "visitor_id": visitor,
                    "request_id": uuid4(),
                    "clicked_at": base + timedelta(seconds=index),
                }
                for index, code_id in enumerate(code_ids)
            ],
        )
        await session.commit()
    response = await register_with_visitor(client, "page@example.com", visitor)
    assert response.status_code == 201
    attribution = await attribution_for(session_maker, response.json()["id"])
    assert attribution is not None and attribution.click_id == click_ids[0]

    from app.modules.referrals.repository import ReferralAttributionRepository

    async with session_maker() as session:
        await session.execute(
            update(ReferralCode).where(ReferralCode.id.in_(code_ids)).values(is_active=True)
        )
        await session.commit()
        user = await session.get(User, UUID(response.json()["id"]))
        repository = ReferralAttributionRepository(session)
        args = (visitor, user.id, user.created_at - timedelta(days=30), user.created_at)
        page_one = await repository.candidates(*args, None)
        assert len(page_one) == 100
        cursor = (page_one[-1][2], page_one[-1][0])
        page_two = await repository.candidates(*args, cursor)
        assert len(page_two) == 1
        assert page_two[0][0] == click_ids[0]
        assert len({row[0] for row in page_one + page_two}) == 101


async def test_codes_filled_after_candidate_query_do_not_deadlock(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    from app.modules.referrals.repository import ReferralAttributionRepository

    headers, first = await make_code(client, "owner@example.com", max_uses=1)
    second = (await client.post(CODES, headers=headers, json={"max_uses": 1})).json()
    fill_clicks = []
    fill_users = []
    for index, code in enumerate((first, second)):
        visitor = uuid4()
        fill_clicks.append(await click(client, code["code"], visitor))
        fill_users.append(
            (await register_with_visitor(client, f"seed{index}@example.com", None)).json()
        )

    visitor_a, visitor_b = uuid4(), uuid4()
    await click(client, first["code"], visitor_a)
    await click(client, second["code"], visitor_a)
    await click(client, second["code"], visitor_b)
    await click(client, first["code"], visitor_b)

    code_ids = [UUID(first["id"]), UUID(second["id"])]
    attempted = {code_id: asyncio.Event() for code_id in code_ids}
    original_lock = ReferralAttributionRepository.lock_code

    async def observed_lock(self, code_id):
        attempted[code_id].set()
        return await original_lock(self, code_id)

    async with session_maker() as blocker:
        for code_id in code_ids:
            await blocker.execute(
                select(ReferralCode).where(ReferralCode.id == code_id).with_for_update()
            )
        for index, code in enumerate((first, second)):
            blocker.add(
                ReferralAttribution(
                    referral_code_id=code_ids[index],
                    referrer_id=UUID(code["owner_id"]),
                    referred_user_id=UUID(fill_users[index]["id"]),
                    click_id=UUID(fill_clicks[index]["id"]),
                    attribution_model=AttributionModel.LAST_REFERRAL_CLICK,
                )
            )
        await blocker.flush()
        monkeypatch.setattr(ReferralAttributionRepository, "lock_code", observed_lock)
        tasks = asyncio.gather(
            register_with_visitor(client, "race_a@example.com", visitor_a),
            register_with_visitor(client, "race_b@example.com", visitor_b),
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in attempted.values())), timeout=15
            )
        finally:
            await blocker.commit()
        results = await asyncio.wait_for(tasks, timeout=15)
    assert [result.status_code for result in results] == [201, 201]
    for result in results:
        assert await attribution_for(session_maker, result.json()["id"]) is None
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ReferralAttribution)) == 2
