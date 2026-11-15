import asyncio
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.leads.models import Lead, LeadStatusHistory
from app.modules.users.models import UserRole
from tests.integration.test_lead_creation import own_user_id
from tests.integration.test_referral_codes import authenticated
from tests.integration.test_referral_programs import staff_headers

URL = "/api/v1/leads"


async def test_status_permissions_and_conversion(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    customer = await authenticated(client, "customer@example.com")
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    manager_id = await own_user_id(client, manager)
    lead = (await client.post(URL, headers=customer, json={"source": "site"})).json()
    url = f"{URL}/{lead['id']}/status"
    assert (await client.patch(url, json={"status": "contacted"})).status_code == 401
    assert (
        await client.patch(url, headers=customer, json={"status": "contacted"})
    ).status_code == 404
    assert (
        await client.patch(url, headers=manager, json={"status": "contacted"})
    ).status_code == 404
    async with session_maker() as session:
        await session.execute(
            update(Lead).where(Lead.id == UUID(lead["id"])).values(assigned_manager_id=manager_id)
        )
        await session.commit()
    assert (
        await client.patch(url, headers=manager, json={"status": "converted"})
    ).status_code == 409
    assert (
        await client.patch(url, headers=manager, json={"status": "contacted"})
    ).status_code == 200
    qualified = await client.patch(url, headers=manager, json={"status": "qualified"})
    assert qualified.status_code == 200 and qualified.json()["qualified_at"] is not None
    results = await asyncio.gather(
        *(client.patch(url, headers=admin, json={"status": "converted"}) for _ in range(2))
    )
    assert sorted(result.status_code for result in results) == [200, 409]
    assert (
        next(result for result in results if result.status_code == 200).json()["converted_at"]
        is not None
    )
    async with session_maker() as session:
        history = (
            await session.scalars(
                select(LeadStatusHistory).where(LeadStatusHistory.lead_id == UUID(lead["id"]))
            )
        ).all()
        assert len(history) == 4
        assert sum(item.new_status == "converted" for item in history) == 1


async def test_lost_is_final_and_history_uses_same_timestamp(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    customer = await authenticated(client, "customer@example.com")
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    lead = (await client.post(URL, headers=customer, json={"source": "site"})).json()
    url = f"{URL}/{lead['id']}/status"
    result = await client.patch(url, headers=admin, json={"status": "lost"})
    assert result.status_code == 200
    assert (await client.patch(url, headers=admin, json={"status": "contacted"})).status_code == 409
    async with session_maker() as session:
        stored = await session.get(Lead, UUID(lead["id"]))
        entries = (
            await session.scalars(
                select(LeadStatusHistory).where(LeadStatusHistory.lead_id == stored.id)
            )
        ).all()
        lost = next(item for item in entries if item.new_status == "lost")
        assert len(entries) == 2 and lost.changed_at == stored.updated_at


async def test_history_failure_rolls_back_status(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    import pytest
    from sqlalchemy.exc import IntegrityError

    from app.modules.leads.models import LeadStatus
    from app.modules.leads.repository import LeadRepository

    customer = await authenticated(client, "customer@example.com")
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    lead = (await client.post(URL, headers=customer, json={"source": "site"})).json()

    def invalid(self, lead_id, user_id, previous, new, changed_at):
        self.session.add(
            LeadStatusHistory(
                lead_id=lead_id,
                changed_by=user_id,
                previous_status=LeadStatus.NEW,
                new_status=LeadStatus.NEW,
            )
        )

    monkeypatch.setattr(LeadRepository, "add_status_history", invalid)
    with pytest.raises(IntegrityError):
        await client.patch(
            f"{URL}/{lead['id']}/status", headers=admin, json={"status": "contacted"}
        )
    async with session_maker() as session:
        stored = await session.get(Lead, UUID(lead["id"]))
        entries = (
            await session.scalars(
                select(LeadStatusHistory).where(LeadStatusHistory.lead_id == stored.id)
            )
        ).all()
        assert stored.status == "new" and len(entries) == 1
