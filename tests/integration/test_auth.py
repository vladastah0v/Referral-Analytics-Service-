import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import hash_refresh_token, password_needs_rehash, verify_password
from app.modules.auth.models import AuthSession
from app.modules.users.models import User, UserRole
from app.modules.users.schemas import UserCreate
from app.modules.users.service import UserService

PASSWORD = "A-long-secure-password!"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/users/me"


async def register(client: AsyncClient, email: str = "user@example.com"):
    return await client.post(
        REGISTER_URL, json={"email": email, "full_name": "  Test User  ", "password": PASSWORD}
    )


async def login(client: AsyncClient, email: str = "user@example.com"):
    return await client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_register_normalizes_and_hashes_password(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    response = await register(client, "User@EXAMPLE.COM")
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "user@example.com"
    assert data["full_name"] == "Test User"
    assert data["role"] == "customer"
    assert "password" not in data and "password_hash" not in data
    async with session_maker() as session:
        user = await session.scalar(select(User))
        assert user is not None
        assert user.password_hash.startswith("$argon2id$")
        assert PASSWORD not in user.password_hash
        assert await verify_password(user.password_hash, PASSWORD)
    duplicate = await register(client, "USER@example.com")
    assert duplicate.status_code == 409


async def test_registration_rejects_role_and_invalid_input(client: AsyncClient) -> None:
    payload = {"email": "user@example.com", "full_name": "Test User", "password": PASSWORD}
    for role in ("admin", "manager", "customer"):
        response = await client.post(REGISTER_URL, json={**payload, "role": role})
        assert response.status_code == 422
    assert (
        await client.post(REGISTER_URL, json={**payload, "password": "short"})
    ).status_code == 422
    assert (
        await client.post(REGISTER_URL, json={**payload, "full_name": "   "})
    ).status_code == 422
    assert (
        await client.post(REGISTER_URL, json={**payload, "email": "invalid"})
    ).status_code == 422
    secret_input = "tiny-secret"
    invalid = await client.post(REGISTER_URL, json={**payload, "password": secret_input})
    assert invalid.status_code == 422 and secret_input not in invalid.text
    assert (await register(client)).status_code == 201


async def test_concurrent_duplicate_registration(client: AsyncClient) -> None:
    results = await asyncio.gather(register(client), register(client, "USER@example.com"))
    assert sorted(response.status_code for response in results) == [201, 409]


async def test_login_and_token_storage(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await register(client)
    response = await login(client, "USER@example.com")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    tokens = response.json()
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] > 0
    assert (await client.get(ME_URL, headers=bearer(tokens["access_token"]))).json()["email"] == (
        "user@example.com"
    )
    async with session_maker() as session:
        saved = await session.scalar(select(AuthSession))
        assert saved is not None
        assert saved.refresh_token_hash == hash_refresh_token(tokens["refresh_token"])
        assert tokens["refresh_token"] not in saved.refresh_token_hash
    assert (await client.get(ME_URL)).status_code == 401
    assert (await client.get(ME_URL, headers=bearer(tokens["refresh_token"]))).status_code == 401
    assert (
        await client.post(LOGIN_URL, json={"email": "user@example.com", "password": "wrong"})
    ).status_code == 401
    assert (
        await client.post(LOGIN_URL, json={"email": "missing@example.com", "password": "wrong"})
    ).status_code == 401


async def test_parallel_logins_upgrade_an_old_password_hash(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    assert (await register(client)).status_code == 201
    old_hash = PasswordHasher(time_cost=1).hash(PASSWORD)
    async with session_maker() as session:
        await session.execute(update(User).values(password_hash=old_hash))
        await session.commit()

    results = await asyncio.gather(login(client), login(client))
    assert [response.status_code for response in results] == [200, 200]
    async with session_maker() as session:
        upgraded = await session.scalar(select(User.password_hash))
        assert upgraded != old_hash
        assert not password_needs_rehash(upgraded)


async def test_rotation_replay_logout_and_session_isolation(client: AsyncClient) -> None:
    await register(client)
    first = (await login(client)).json()
    other = (await login(client)).json()
    result = await client.post(REFRESH_URL, json={"refresh_token": first["refresh_token"]})
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    second = result.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert (
        await client.post(REFRESH_URL, json={"refresh_token": first["refresh_token"]})
    ).status_code == 401
    assert (
        await client.post(LOGOUT_URL, json={"refresh_token": first["refresh_token"]})
    ).status_code == 401
    assert (await client.get(ME_URL, headers=bearer(second["access_token"]))).status_code == 200
    assert (
        await client.post(LOGOUT_URL, json={"refresh_token": second["refresh_token"]})
    ).status_code == 204
    assert (await client.get(ME_URL, headers=bearer(first["access_token"]))).status_code == 401
    assert (await client.get(ME_URL, headers=bearer(second["access_token"]))).status_code == 401
    assert (
        await client.post(REFRESH_URL, json={"refresh_token": second["refresh_token"]})
    ).status_code == 401
    assert (await client.get(ME_URL, headers=bearer(other["access_token"]))).status_code == 200


async def test_concurrent_refresh_has_exactly_one_winner(client: AsyncClient) -> None:
    await register(client)
    token = (await login(client)).json()["refresh_token"]
    responses = await asyncio.gather(
        client.post(REFRESH_URL, json={"refresh_token": token}),
        client.post(REFRESH_URL, json={"refresh_token": token}),
    )
    assert sorted(response.status_code for response in responses) == [200, 401]
    winner = next(response.json() for response in responses if response.status_code == 200)
    assert (await client.get(ME_URL, headers=bearer(winner["access_token"]))).status_code == 200


async def test_disabled_user_and_expired_session_are_rejected(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await register(client)
    tokens = (await login(client)).json()
    async with session_maker() as session:
        await session.execute(update(User).values(is_active=False))
        await session.commit()
    assert (await client.get(ME_URL, headers=bearer(tokens["access_token"]))).status_code == 401
    assert (
        await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 401
    assert (await login(client)).status_code == 401
    async with session_maker() as session:
        await session.execute(update(User).values(is_active=True))
        await session.execute(
            update(AuthSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    assert (await client.get(ME_URL, headers=bearer(tokens["access_token"]))).status_code == 401
    assert (
        await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 401


@pytest.mark.parametrize(
    ("role", "admin_status", "staff_status"),
    [(UserRole.CUSTOMER, 403, 403), (UserRole.MANAGER, 403, 200), (UserRole.ADMIN, 200, 200)],
)
async def test_role_enforcement(
    role: UserRole,
    admin_status: int,
    staff_status: int,
    client: AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    data = UserCreate(email="staff@example.com", full_name="Staff", password=PASSWORD)
    async with session_maker() as session:
        await UserService(session).create(data, role)
    token = (await login(client, "staff@example.com")).json()["access_token"]
    assert (await client.get("/test/admin", headers=bearer(token))).status_code == admin_status
    assert (await client.get("/test/staff", headers=bearer(token))).status_code == staff_status


async def test_admin_creation_service_uses_admin_role(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    from app import cli

    monkeypatch.setattr(cli, "session_factory", session_maker)
    await cli.create_admin("admin@example.com", "Administrator", PASSWORD)
    token = (await login(client, "admin@example.com")).json()["access_token"]
    assert (await client.get("/test/admin", headers=bearer(token))).status_code == 200


async def test_login_limit_rejects_excess_attempts_before_password_verification(
    client: AsyncClient,
) -> None:
    if not os.getenv("TEST_REDIS_URL"):
        pytest.skip("Set TEST_REDIS_URL to exercise the Redis authentication limiter")
    for _ in range(30):
        assert (await client.post("/api/v1/auth/login", json={})).status_code == 422
    limited = await client.post("/api/v1/auth/login", json={})
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
