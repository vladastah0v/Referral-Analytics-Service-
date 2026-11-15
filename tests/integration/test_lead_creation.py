import asyncio
from uuid import UUID, uuid1, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.leads.models import Lead, LeadStatus, LeadStatusHistory
from app.modules.leads.repository import LeadRepository
from app.modules.referrals.models import ReferralAttribution
from app.modules.referrals.repository import ReferralAttributionRepository
from app.modules.referrals.service import ReferralAttributionService
from app.modules.users.models import UserRole
from tests.integration.test_auth import bearer, login
from tests.integration.test_referral_attribution import click, make_code, register_with_visitor
from tests.integration.test_referral_codes import authenticated
from tests.integration.test_referral_programs import staff_headers

URL = "/api/v1/leads"


async def own_user_id(client: AsyncClient, headers: dict[str, str]) -> UUID:
    response = await client.get("/api/v1/users/me", headers=headers)
    assert response.status_code == 200
    return UUID(response.json()["id"])


async def counts(session_maker: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with session_maker() as session:
        leads = await session.scalar(select(func.count()).select_from(Lead))
        history = await session.scalar(select(func.count()).select_from(LeadStatusHistory))
        return leads, history


async def test_create_lead_with_automatically_associated_attribution(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, code = await make_code(client, "referrer@example.com")
    visitor_id = uuid4()
    await click(client, code["code"], visitor_id)
    registered = await register_with_visitor(client, "customer@example.com", visitor_id)
    assert registered.status_code == 201
    customer = bearer((await login(client, "customer@example.com")).json()["access_token"])
    response = await client.post(URL, headers=customer, json={"source": "  website  "})
    assert response.status_code == 201
    assert response.headers["cache-control"] == "private, no-store"
    data = response.json()
    assert data["user_id"] == registered.json()["id"]
    assert data["source"] == "website" and data["status"] == "new"
    assert data["assigned_manager_id"] is None
    assert data["qualified_at"] is None and data["converted_at"] is None
    assert "creation_request_id" not in data and "creation_request_hash" not in data
    async with session_maker() as session:
        attribution = await session.scalar(
            select(ReferralAttribution).where(
                ReferralAttribution.referred_user_id == UUID(data["user_id"])
            )
        )
        assert attribution is not None and data["referral_attribution_id"] == str(attribution.id)
        history = (await session.scalars(select(LeadStatusHistory))).all()
        assert len(history) == 1
        assert history[0].lead_id == UUID(data["id"])
        assert history[0].changed_by == UUID(data["user_id"])
        assert history[0].previous_status is None and history[0].new_status == LeadStatus.NEW


async def test_create_without_attribution_and_reject_forged_fields(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    customer = await authenticated(client, "customer@example.com")
    assert (await client.post(URL, json={"source": "site"})).status_code == 401
    for payload in (
        {},
        {"source": ""},
        {"source": "  \t\n"},
        {"source": "x" * 129},
        {"source": "site", "user_id": str(uuid4())},
        {"source": "site", "referral_attribution_id": str(uuid4())},
        {"source": "site", "assigned_manager_id": str(uuid4())},
        {"source": "site", "status": "converted"},
        {"source": "site", "creation_request_id": str(uuid4())},
    ):
        assert (await client.post(URL, headers=customer, json=payload)).status_code == 422
    for value in ("not-a-uuid", str(uuid1())):
        assert (
            await client.post(
                URL, headers=customer | {"Idempotency-Key": value}, json={"source": "site"}
            )
        ).status_code == 422
    result = await client.post(URL, headers=customer, json={"source": "site"})
    assert result.status_code == 201
    assert result.json()["referral_attribution_id"] is None
    assert await counts(session_maker) == (1, 1)


@pytest.mark.parametrize("role", [UserRole.MANAGER, UserRole.ADMIN])
async def test_staff_can_only_create_for_themselves(
    role: UserRole, client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    headers = await staff_headers(client, session_maker, role)
    user_id = await own_user_id(client, headers)
    result = await client.post(URL, headers=headers, json={"source": "site"})
    assert result.status_code == 201 and result.json()["user_id"] == str(user_id)
    assert (
        await client.post(URL, headers=headers, json={"source": "site", "user_id": str(uuid4())})
    ).status_code == 422


async def test_idempotency_replay_conflict_and_user_scope(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    first = await authenticated(client, "first@example.com")
    second = await authenticated(client, "second@example.com")
    key = str(uuid4())
    first_headers = first | {"Idempotency-Key": key}
    initial = await client.post(URL, headers=first_headers, json={"source": "  website  "})
    assert initial.status_code == 201
    replay = await client.post(URL, headers=first_headers, json={"source": "website"})
    assert replay.status_code == 200 and replay.json() == initial.json()
    assert (
        await client.post(URL, headers=first_headers, json={"source": "different"})
    ).status_code == 409
    other = await client.post(
        URL, headers=second | {"Idempotency-Key": key}, json={"source": "other"}
    )
    assert other.status_code == 201 and other.json()["id"] != initial.json()["id"]
    assert await counts(session_maker) == (2, 2)
    async with session_maker() as session:
        await session.execute(
            update(Lead).where(Lead.id == UUID(initial.json()["id"])).values(source="updated")
        )
        await session.commit()
    replay = await client.post(URL, headers=first_headers, json={"source": "website"})
    assert replay.status_code == 200 and replay.json()["source"] == "updated"
    assert await counts(session_maker) == (2, 2)


async def test_simultaneous_retries_create_one_lead_and_one_history(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    headers = await authenticated(client, "customer@example.com")
    headers |= {"Idempotency-Key": str(uuid4())}
    responses = await asyncio.gather(
        *(client.post(URL, headers=headers, json={"source": "site"}) for _ in range(8))
    )
    assert sorted(response.status_code for response in responses) == [200] * 7 + [201]
    assert len({response.json()["id"] for response in responses}) == 1
    assert await counts(session_maker) == (1, 1)


async def test_requests_without_key_are_distinct(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    headers = await authenticated(client, "customer@example.com")
    responses = await asyncio.gather(
        *(client.post(URL, headers=headers, json={"source": "site"}) for _ in range(2))
    )
    assert [response.status_code for response in responses] == [201, 201]
    assert len({response.json()["id"] for response in responses}) == 2
    assert await counts(session_maker) == (2, 2)


async def test_history_failure_rolls_back_lead_and_key(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    headers = await authenticated(client, "customer@example.com")
    headers |= {"Idempotency-Key": str(uuid4())}
    original = LeadRepository.add_initial_history

    def invalid_history(self, lead_id, user_id):
        self.session.add(
            LeadStatusHistory(
                lead_id=lead_id,
                changed_by=user_id,
                previous_status=LeadStatus.NEW,
                new_status=LeadStatus.NEW,
            )
        )

    monkeypatch.setattr(LeadRepository, "add_initial_history", invalid_history)
    with pytest.raises(IntegrityError):
        await client.post(URL, headers=headers, json={"source": "site"})
    assert await counts(session_maker) == (0, 0)
    monkeypatch.setattr(LeadRepository, "add_initial_history", original)
    assert (await client.post(URL, headers=headers, json={"source": "site"})).status_code == 201
    assert await counts(session_maker) == (1, 1)


async def test_creation_waits_for_pending_attribution(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    _, code = await make_code(client, "referrer@example.com")
    visitor = uuid4()
    await click(client, code["code"], visitor)
    registered = await register_with_visitor(client, "customer@example.com", None)
    user_id = UUID(registered.json()["id"])
    headers = bearer((await login(client, "customer@example.com")).json()["access_token"])
    lock_attempted = asyncio.Event()
    original = ReferralAttributionRepository.lock_user

    async def observed_lock(self, candidate_id):
        lock_attempted.set()
        await original(self, candidate_id)

    async with session_maker() as blocker:
        attribution = await ReferralAttributionService(blocker).attribute_registration(
            user_id, visitor
        )
        assert attribution is not None
        monkeypatch.setattr(ReferralAttributionRepository, "lock_user", observed_lock)
        task = asyncio.create_task(client.post(URL, headers=headers, json={"source": "site"}))
        try:
            await asyncio.wait_for(lock_attempted.wait(), timeout=10)
            assert not task.done()
        finally:
            await blocker.commit()
        result = await asyncio.wait_for(task, timeout=10)
    assert result.status_code == 201
    assert result.json()["referral_attribution_id"] == str(attribution.id)
    assert await counts(session_maker) == (1, 1)
