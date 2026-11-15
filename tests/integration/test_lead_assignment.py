from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.leads.models import LeadStatusHistory
from app.modules.users.models import User, UserRole
from tests.integration.test_lead_creation import own_user_id
from tests.integration.test_referral_codes import authenticated
from tests.integration.test_referral_programs import staff_headers

URL = "/api/v1/leads"


async def test_assignment_permissions_validation_and_visibility(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    customer = await authenticated(client, "customer@example.com")
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    manager_id = await own_user_id(client, manager)
    customer_id = await own_user_id(client, customer)
    admin_id = await own_user_id(client, admin)
    lead = (await client.post(URL, headers=customer, json={"source": "site"})).json()
    detail = f"{URL}/{lead['id']}"
    assignment = detail + "/assign"
    assert (await client.patch(assignment, json={"manager_id": str(manager_id)})).status_code == 401
    for headers in (customer, manager):
        assert (
            await client.patch(assignment, headers=headers, json={"manager_id": str(manager_id)})
        ).status_code == 403
    for invalid in (customer_id, admin_id, uuid4()):
        assert (
            await client.patch(assignment, headers=admin, json={"manager_id": str(invalid)})
        ).status_code == 422
    assert (await client.patch(assignment, headers=admin, json={})).status_code == 422
    assert (
        await client.patch(f"{URL}/{uuid4()}/assign", headers=admin, json={"manager_id": None})
    ).status_code == 404
    result = await client.patch(assignment, headers=admin, json={"manager_id": str(manager_id)})
    assert result.status_code == 200 and result.json()["assigned_manager_id"] == str(manager_id)
    assert (await client.get(detail, headers=manager)).status_code == 200
    replay = await client.patch(assignment, headers=admin, json={"manager_id": str(manager_id)})
    assert replay.status_code == 200 and replay.json()["updated_at"] == result.json()["updated_at"]
    assert (
        await client.patch(assignment, headers=admin, json={"manager_id": None})
    ).status_code == 200
    assert (await client.get(detail, headers=manager)).status_code == 404
    async with session_maker() as session:
        history = (await session.scalars(select(LeadStatusHistory))).all()
        assert len(history) == 1
        await session.execute(update(User).where(User.id == manager_id).values(is_active=False))
        await session.commit()
    assert (
        await client.patch(assignment, headers=admin, json={"manager_id": str(manager_id)})
    ).status_code == 422


async def test_concurrent_reassignment_has_consistent_access(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    import asyncio

    customer = await authenticated(client, "customer@example.com")
    manager_one = await staff_headers(client, session_maker, UserRole.MANAGER)
    manager_two = await authenticated(client, "second@example.com")
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    first_id = await own_user_id(client, manager_one)
    second_id = await own_user_id(client, manager_two)
    async with session_maker() as session:
        await session.execute(
            update(User).where(User.id == second_id).values(role=UserRole.MANAGER)
        )
        await session.commit()
    lead = (await client.post(URL, headers=customer, json={"source": "site"})).json()
    detail = f"{URL}/{lead['id']}"
    results = await asyncio.gather(
        client.patch(detail + "/assign", headers=admin, json={"manager_id": str(first_id)}),
        client.patch(detail + "/assign", headers=admin, json={"manager_id": str(second_id)}),
    )
    assert [result.status_code for result in results] == [200, 200]
    stored = (await client.get(detail, headers=admin)).json()["assigned_manager_id"]
    first_status = (await client.get(detail, headers=manager_one)).status_code
    second_status = (await client.get(detail, headers=manager_two)).status_code
    assert (first_status, second_status) == ((200, 404) if stored == str(first_id) else (404, 200))
