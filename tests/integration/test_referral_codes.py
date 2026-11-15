from datetime import UTC, datetime, timedelta
from uuid import uuid4

from httpx import AsyncClient

from app.modules.referrals import service
from tests.integration.test_auth import bearer, login, register

URL = "/api/v1/referrals/codes"


async def authenticated(client: AsyncClient, email: str) -> dict[str, str]:
    assert (await register(client, email)).status_code == 201
    return bearer((await login(client, email)).json()["access_token"])


async def test_creation_and_owner_isolation(client: AsyncClient) -> None:
    first = await authenticated(client, "first@example.com")
    second = await authenticated(client, "second@example.com")
    expiry = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    result = await client.post(URL, headers=first, json={"expires_at": expiry, "max_uses": 5})
    assert result.status_code == 201
    created = result.json()
    assert created["code"] and created["is_active"] is True
    assert created["max_uses"] == 5 and created["expires_at"] is not None
    assert (await client.post(URL, headers=second, json={})).status_code == 201
    first_page = (await client.get(URL, headers=first)).json()
    second_page = (await client.get(URL, headers=second)).json()
    assert [item["id"] for item in first_page["items"]] == [created["id"]]
    assert created["id"] not in [item["id"] for item in second_page["items"]]
    assert (await client.post(URL, json={})).status_code == 401
    assert (await client.get(URL)).status_code == 401


async def test_creation_validation_and_pagination(client: AsyncClient) -> None:
    headers = await authenticated(client, "user@example.com")
    for payload in (
        {"max_uses": 0},
        {"max_uses": True},
        {"max_uses": 2_147_483_648},
        {"expires_at": "2020-01-01T00:00:00Z"},
        {"expires_at": "2099-01-01T00:00:00"},
        {"owner_id": str(uuid4())},
        {"code": "client-controlled"},
    ):
        assert (await client.post(URL, headers=headers, json=payload)).status_code == 422
    first = (await client.post(URL, headers=headers, json={})).json()
    second = (await client.post(URL, headers=headers, json={})).json()
    page_one = (await client.get(URL, headers=headers, params={"limit": 1})).json()
    page_two = (await client.get(URL, headers=headers, params={"limit": 1, "offset": 1})).json()
    assert {page_one["items"][0]["id"], page_two["items"][0]["id"]} == {first["id"], second["id"]}
    assert page_one["limit"] == 1 and page_two["offset"] == 1
    assert (await client.get(URL, headers=headers, params={"limit": 101})).status_code == 422
    assert (await client.get(URL, headers=headers, params={"offset": -1})).status_code == 422


async def test_generated_code_collision_is_retried(client: AsyncClient, monkeypatch) -> None:
    headers = await authenticated(client, "user@example.com")
    generated = iter(["existing-code", "existing-code", "new-code"])
    monkeypatch.setattr(service.secrets, "token_urlsafe", lambda size: next(generated))
    first = await client.post(URL, headers=headers, json={})
    second = await client.post(URL, headers=headers, json={})
    assert first.status_code == second.status_code == 201
    assert first.json()["code"] == "existing-code"
    assert second.json()["code"] == "new-code"


async def test_detail_update_and_owner_access(client: AsyncClient) -> None:
    owner = await authenticated(client, "owner@example.com")
    other = await authenticated(client, "other@example.com")
    created = (await client.post(URL, headers=owner, json={"max_uses": 5})).json()
    code_url = f"{URL}/{created['id']}"
    assert (await client.get(code_url, headers=owner)).json() == created
    for headers in (other,):
        assert (await client.get(code_url, headers=headers)).status_code == 404
        assert (
            await client.patch(code_url, headers=headers, json={"is_active": False})
        ).status_code == 404
    missing_url = f"{URL}/{uuid4()}"
    assert (await client.get(missing_url, headers=owner)).status_code == 404
    assert (await client.patch(missing_url, headers=owner, json={"max_uses": 2})).status_code == 404
    assert (await client.get(code_url)).status_code == 401
    assert (await client.patch(code_url, json={"max_uses": 2})).status_code == 401
    assert (await client.get(f"{URL}/invalid", headers=owner)).status_code == 422
    expiry = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    result = await client.patch(
        code_url, headers=owner, json={"expires_at": expiry, "max_uses": 10}
    )
    assert result.status_code == 200
    assert result.json()["max_uses"] == 10 and result.json()["expires_at"] is not None
    result = await client.patch(
        code_url, headers=owner, json={"expires_at": None, "max_uses": None}
    )
    assert result.json()["expires_at"] is None and result.json()["max_uses"] is None
    result = await client.patch(code_url, headers=owner, json={"is_active": False})
    assert result.status_code == 200 and result.json()["is_active"] is False
    assert (await client.get(code_url, headers=owner)).json()["is_active"] is False


async def test_update_rejects_forbidden_or_invalid_values(client: AsyncClient) -> None:
    headers = await authenticated(client, "owner@example.com")
    created = (await client.post(URL, headers=headers, json={})).json()
    code_url = f"{URL}/{created['id']}"
    for payload in (
        {},
        {"is_active": True},
        {"is_active": None},
        {"is_active": 0},
        {"max_uses": 0},
        {"max_uses": True},
        {"expires_at": "2020-01-01T00:00:00Z"},
        {"expires_at": "2099-01-01T00:00:00"},
        {"owner_id": str(uuid4())},
        {"code": "other"},
    ):
        assert (await client.patch(code_url, headers=headers, json=payload)).status_code == 422
    assert (await client.get(code_url, headers=headers)).json() == created


async def test_concurrent_updates_preserve_distinct_fields(client: AsyncClient) -> None:
    import asyncio

    headers = await authenticated(client, "owner@example.com")
    created = (await client.post(URL, headers=headers, json={})).json()
    code_url = f"{URL}/{created['id']}"
    results = await asyncio.gather(
        client.patch(code_url, headers=headers, json={"is_active": False}),
        client.patch(code_url, headers=headers, json={"max_uses": 8}),
    )
    assert [response.status_code for response in results] == [200, 200]
    current = (await client.get(code_url, headers=headers)).json()
    assert current["is_active"] is False and current["max_uses"] == 8
