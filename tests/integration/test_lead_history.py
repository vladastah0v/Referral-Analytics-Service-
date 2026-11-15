import asyncio
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.leads.repository import LeadRepository
from app.modules.users.models import UserRole
from tests.integration.test_lead_creation import own_user_id
from tests.integration.test_referral_codes import authenticated
from tests.integration.test_referral_programs import staff_headers

URL = "/api/v1/leads"


async def test_history_access_and_pagination(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    owner = await authenticated(client, "owner@example.com")
    other = await authenticated(client, "other@example.com")
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    manager_id = await own_user_id(client, manager)
    lead = (await client.post(URL, headers=owner, json={"source": "site"})).json()
    history = f"{URL}/{lead['id']}/history"
    assert (await client.get(history)).status_code == 401
    assert (await client.get(history, headers=other)).status_code == 404
    assert (await client.get(history, headers=manager)).status_code == 404
    assert (await client.get(f"{URL}/{uuid4()}/history", headers=admin)).status_code == 404
    initial = await client.get(history, headers=owner)
    assert initial.status_code == 200 and initial.headers["cache-control"] == "private, no-store"
    assert len(initial.json()["items"]) == 1
    assert initial.json()["items"][0]["previous_status"] is None
    assert initial.json()["items"][0]["new_status"] == "new"
    assert (
        await client.patch(
            f"{URL}/{lead['id']}/assign", headers=admin, json={"manager_id": str(manager_id)}
        )
    ).status_code == 200
    assert (
        await client.patch(
            f"{URL}/{lead['id']}/status", headers=manager, json={"status": "contacted"}
        )
    ).status_code == 200
    page1 = (await client.get(history, headers=manager, params={"limit": 1})).json()
    page2 = (await client.get(history, headers=manager, params={"limit": 1, "offset": 1})).json()
    assert page1["items"][0]["new_status"] == "new"
    assert page2["items"][0]["previous_status"] == "new"
    assert page2["items"][0]["new_status"] == "contacted"
    assert page2["items"][0]["changed_by"] == str(manager_id)
    assert (await client.get(history, headers=admin, params={"limit": 1, "offset": 2})).json()[
        "items"
    ] == []
    assert (await client.get(history, headers=owner, params={"limit": 101})).status_code == 422
    assert (await client.get(history, headers=owner, params={"offset": -1})).status_code == 422
    assert (
        await client.patch(f"{URL}/{lead['id']}/assign", headers=admin, json={"manager_id": None})
    ).status_code == 200
    assert (await client.get(history, headers=manager)).status_code == 404
    assert len((await client.get(history, headers=admin)).json()["items"]) == 2


async def test_history_read_serializes_with_unassignment(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    owner = await authenticated(client, "owner@example.com")
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    manager_id = await own_user_id(client, manager)
    lead = (await client.post(URL, headers=owner, json={"source": "site"})).json()
    assignment = f"{URL}/{lead['id']}/assign"
    history = f"{URL}/{lead['id']}/history"
    assert (
        await client.patch(assignment, headers=admin, json={"manager_id": str(manager_id)})
    ).status_code == 200
    entered, release, assign_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = LeadRepository.list_status_history
    original_assign = LeadRepository.lock_for_assignment

    async def delayed(self, lead_id, limit, offset):
        entered.set()
        await release.wait()
        return await original(self, lead_id, limit, offset)

    async def observed_assignment(self, lead_id):
        assign_entered.set()
        return await original_assign(self, lead_id)

    monkeypatch.setattr(LeadRepository, "list_status_history", delayed)
    monkeypatch.setattr(LeadRepository, "lock_for_assignment", observed_assignment)
    read_task = asyncio.create_task(client.get(history, headers=manager))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        change_task = asyncio.create_task(
            client.patch(assignment, headers=admin, json={"manager_id": None})
        )
        await asyncio.wait_for(assign_entered.wait(), timeout=10)
        await asyncio.sleep(0.1)
        assert not change_task.done()
    finally:
        release.set()
    assert (await asyncio.wait_for(read_task, timeout=10)).status_code == 200
    assert (await asyncio.wait_for(change_task, timeout=10)).status_code == 200
    assert (await client.get(history, headers=manager)).status_code == 404
