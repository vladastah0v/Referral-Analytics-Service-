from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.leads.models import Lead
from app.modules.users.models import UserRole
from tests.integration.test_lead_creation import own_user_id
from tests.integration.test_referral_codes import authenticated
from tests.integration.test_referral_programs import staff_headers

URL = "/api/v1/leads"


async def test_visibility_and_filters(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    first = await authenticated(client, "first@example.com")
    second = await authenticated(client, "second@example.com")
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    manager_id = await own_user_id(client, manager)
    a = (await client.post(URL, headers=first, json={"source": "a"})).json()
    b = (await client.post(URL, headers=second, json={"source": "b"})).json()
    async with session_maker() as session:
        await session.execute(
            update(Lead).where(Lead.id == UUID(a["id"])).values(assigned_manager_id=manager_id)
        )
        await session.commit()
    assert (await client.get(URL)).status_code == 401
    assert [row["id"] for row in (await client.get(URL, headers=first)).json()["items"]] == [
        a["id"]
    ]
    assert [row["id"] for row in (await client.get(URL, headers=manager)).json()["items"]] == [
        a["id"]
    ]
    assert len((await client.get(URL, headers=admin)).json()["items"]) == 2
    assert (await client.get(f"{URL}/{b['id']}", headers=first)).status_code == 404
    assert (await client.get(f"{URL}/{b['id']}", headers=manager)).status_code == 404
    assert (await client.get(f"{URL}/{b['id']}", headers=admin)).status_code == 200
    assert (
        len(
            (
                await client.get(
                    URL, headers=admin, params={"manager_id": str(manager_id), "status": "new"}
                )
            ).json()["items"]
        )
        == 1
    )
    assert (await client.get(URL, headers=admin, params={"limit": 1})).json()["limit"] == 1
    assert (await client.get(URL, headers=admin, params={"status": "bad"})).status_code == 422
    assert (
        await client.get(
            URL,
            headers=admin,
            params={"date_from": "2026-01-02T00:00:00Z", "date_to": "2026-01-01T00:00:00Z"},
        )
    ).status_code == 422


async def test_period_sort_and_pagination(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from datetime import UTC, datetime

    headers = await authenticated(client, "dates@example.com")
    first = (await client.post(URL, headers=headers, json={"source": "first"})).json()
    second = (await client.post(URL, headers=headers, json={"source": "second"})).json()
    async with session_maker() as session:
        await session.execute(
            update(Lead)
            .where(Lead.id == UUID(first["id"]))
            .values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
        await session.execute(
            update(Lead)
            .where(Lead.id == UUID(second["id"]))
            .values(created_at=datetime(2026, 1, 2, tzinfo=UTC))
        )
        await session.commit()
    params = {"sort": "asc", "limit": 1}
    page1 = (await client.get(URL, headers=headers, params=params)).json()
    page2 = (await client.get(URL, headers=headers, params=params | {"offset": 1})).json()
    assert page1["items"][0]["id"] == first["id"]
    assert page2["items"][0]["id"] == second["id"]
    result = await client.get(
        URL,
        headers=headers,
        params={"date_from": "2026-01-01T05:00:00+05:00", "date_to": "2026-01-02T00:00:00Z"},
    )
    assert [row["id"] for row in result.json()["items"]] == [first["id"]]
    assert (
        await client.get(URL, headers=headers, params={"date_from": "2026-01-01T00:00:00"})
    ).status_code == 422
