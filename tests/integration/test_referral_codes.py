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
