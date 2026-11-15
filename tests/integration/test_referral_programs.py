import asyncio
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.users.models import UserRole
from app.modules.users.schemas import UserCreate
from app.modules.users.service import UserService
from tests.integration.test_auth import PASSWORD, bearer, login
from tests.integration.test_referral_codes import authenticated

URL = "/api/v1/admin/referral-programs"
PAYLOAD = {
    "name": "Spring campaign",
    "reward_type": "fixed",
    "reward_value": "150.00",
    "max_reward": "500.00",
    "currency": "KZT",
    "starts_at": "2026-01-01T00:00:00Z",
    "ends_at": "2027-01-01T00:00:00Z",
}


async def staff_headers(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession], role: UserRole
) -> dict[str, str]:
    email = f"{role.value}@example.com"
    async with session_maker() as session:
        await UserService(session).create(
            UserCreate(email=email, full_name="Staff", password=PASSWORD), role
        )
    return bearer((await login(client, email)).json()["access_token"])


async def test_access_requires_admin(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    customer = await authenticated(client, "customer@example.com")
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    for headers, expected in (({}, 401), (customer, 403), (manager, 403)):
        assert (await client.get(URL, headers=headers)).status_code == expected
        assert (await client.post(URL, headers=headers, json=PAYLOAD)).status_code == expected
        assert (
            await client.patch(f"{URL}/{uuid4()}", headers=headers, json={"name": "x"})
        ).status_code == expected
    assert (await client.get(URL, headers=admin)).status_code == 200


async def test_create_list_and_update(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    headers = await staff_headers(client, session_maker, UserRole.ADMIN)
    result = await client.post(URL, headers=headers, json=PAYLOAD)
    assert result.status_code == 201
    program = result.json()
    assert program["reward_value"] == "150.00" and program["max_reward"] == "500.00"
    assert program["is_active"] is True
    assert (await client.get(URL, headers=headers)).json()["items"] == [program]
    second = await client.post(URL, headers=headers, json={**PAYLOAD, "name": "Another"})
    assert second.status_code == 201
    first_page = (await client.get(URL, headers=headers, params={"limit": 1})).json()
    second_page = (await client.get(URL, headers=headers, params={"limit": 1, "offset": 1})).json()
    assert {first_page["items"][0]["id"], second_page["items"][0]["id"]} == {
        program["id"],
        second.json()["id"],
    }
    assert (await client.get(URL, headers=headers, params={"limit": 101})).status_code == 422
    changed = await client.patch(
        f"{URL}/{program['id']}",
        headers=headers,
        json={
            "reward_type": "percentage",
            "reward_value": "12.50",
            "is_active": False,
            "max_reward": None,
            "ends_at": None,
        },
    )
    assert changed.status_code == 200
    assert (
        changed.json()["reward_type"] == "percentage" and changed.json()["reward_value"] == "12.50"
    )
    assert changed.json()["is_active"] is False and changed.json()["max_reward"] is None
    assert changed.json()["ends_at"] is None
    assert (
        await client.patch(f"{URL}/{uuid4()}", headers=headers, json={"name": "x"})
    ).status_code == 404


async def test_validation_of_create_and_combined_updates(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    headers = await staff_headers(client, session_maker, UserRole.ADMIN)
    for changes in (
        {"name": "  "},
        {"reward_type": "unknown"},
        {"reward_value": "0"},
        {"reward_value": "1.001"},
        {"reward_value": "1234567890123.00"},
        {"reward_type": "percentage", "reward_value": "100.01"},
        {"currency": "kzt"},
        {"max_reward": "-1"},
        {"is_active": "false"},
        {"starts_at": "2026-01-01T00:00:00"},
        {"ends_at": "2025-01-01T00:00:00Z"},
        {"extra": "not allowed"},
    ):
        assert (await client.post(URL, headers=headers, json=PAYLOAD | changes)).status_code == 422
    program = (await client.post(URL, headers=headers, json=PAYLOAD)).json()
    program_url = f"{URL}/{program['id']}"
    for changes in (
        {},
        {"reward_type": "percentage"},
        {"ends_at": "2025-01-01T00:00:00Z"},
        {"starts_at": "2028-01-01T00:00:00Z"},
        {"currency": None},
        {"name": ""},
        {"reward_value": "999999999999999999999999.00"},
        {"is_active": None},
    ):
        assert (await client.patch(program_url, headers=headers, json=changes)).status_code == 422
    assert (await client.get(URL, headers=headers)).json()["items"] == [program]


async def test_concurrent_updates_preserve_valid_program(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    headers = await staff_headers(client, session_maker, UserRole.ADMIN)
    program = (await client.post(URL, headers=headers, json=PAYLOAD)).json()
    program_url = f"{URL}/{program['id']}"
    responses = await asyncio.gather(
        client.patch(program_url, headers=headers, json={"reward_type": "percentage"}),
        client.patch(program_url, headers=headers, json={"reward_value": "80.00"}),
    )
    assert responses[1].status_code == 200
    assert responses[0].status_code in (200, 422)
    current = (await client.get(URL, headers=headers)).json()["items"][0]
    assert current["reward_value"] == "80.00"
    assert current["reward_type"] == ("percentage" if responses[0].status_code == 200 else "fixed")
