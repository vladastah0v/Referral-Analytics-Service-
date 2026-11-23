import os
import subprocess
import sys
from collections.abc import AsyncIterator
from ipaddress import IPv6Address
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.exceptions import ConnectionError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.database import get_session
from app.core.dependencies import require_roles
from app.core.exceptions import (
    AuthenticationError,
    EmailAlreadyRegisteredError,
    PermissionDeniedError,
)
from app.main import (
    authentication_error_handler,
    email_error_handler,
    permission_error_handler,
    validation_error_handler,
)
from app.modules.analytics.cache import get_cache_client
from app.modules.analytics.router import router as analytics_router
from app.modules.auth.rate_limit import get_auth_rate_limit_redis
from app.modules.auth.router import router as auth_router
from app.modules.leads.router import router as leads_router
from app.modules.orders.router import router as orders_router
from app.modules.referrals.admin_router import router as referral_admin_router
from app.modules.referrals.router import router as referrals_router
from app.modules.rewards.router import router as rewards_router
from app.modules.tracking.router import router as events_router
from app.modules.users.models import User, UserRole
from app.modules.users.router import router as users_router


@pytest.fixture(scope="session")
def test_database_url() -> str:
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated disposable PostgreSQL test database")
    url = make_url(value)
    if url.drivername != "postgresql+asyncpg" or not (url.database or "").startswith("test_"):
        pytest.fail("TEST_DATABASE_URL must use asyncpg and a dedicated database named test_*")
    env = os.environ.copy()
    env.update(
        DATABASE_URL=value,
        APP_ENV="test",
        JWT_SECRET="integration-testing-secret-at-least-thirty-two-bytes",
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return value


@pytest_asyncio.fixture
async def db_engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE auth_sessions, users, referral_programs, "
                "integration_credentials, analytics_snapshot_requests, "
                "analytics_snapshot_failures, analytics_daily_snapshots CASCADE"
            )
        )
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE TABLE auth_sessions, users, referral_programs, "
                    "integration_credentials, analytics_snapshot_requests, "
                    "analytics_snapshot_failures, analytics_daily_snapshots CASCADE"
                )
            )
        await engine.dispose()


@pytest.fixture
def session_maker(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def auth_rate_limit_store():
    if url := os.environ.get("TEST_REDIS_URL"):
        async with Redis.from_url(url, decode_responses=True) as store:
            yield store
    else:

        class OfflineStore:
            async def eval(self, *_args):
                raise ConnectionError("Redis is not configured for this test")

        yield OfflineStore()


@pytest.fixture
def test_app(session_maker: async_sessionmaker[AsyncSession], auth_rate_limit_store) -> FastAPI:
    api = FastAPI()
    api.include_router(auth_router, prefix="/api/v1")
    api.include_router(orders_router, prefix="/api/v1")
    api.include_router(rewards_router, prefix="/api/v1")
    api.include_router(analytics_router, prefix="/api/v1")
    api.include_router(events_router, prefix="/api/v1")
    api.include_router(leads_router, prefix="/api/v1")
    api.include_router(referrals_router, prefix="/api/v1")
    api.include_router(referral_admin_router, prefix="/api/v1")
    api.include_router(users_router, prefix="/api/v1")
    api.add_exception_handler(RequestValidationError, validation_error_handler)
    api.add_exception_handler(AuthenticationError, authentication_error_handler)
    api.add_exception_handler(PermissionDeniedError, permission_error_handler)
    api.add_exception_handler(EmailAlreadyRegisteredError, email_error_handler)

    async def get_test_session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    api.dependency_overrides[get_session] = get_test_session

    async def test_cache():
        if url := os.environ.get("TEST_REDIS_URL"):
            async with Redis.from_url(url, decode_responses=True) as store:
                yield store
        else:
            yield None

    api.dependency_overrides[get_cache_client] = test_cache

    api.dependency_overrides[get_auth_rate_limit_redis] = lambda: auth_rate_limit_store

    @api.get("/test/admin")
    async def admin_endpoint(
        user: Annotated[User, Depends(require_roles(UserRole.ADMIN))],
    ) -> dict[str, str]:
        return {"role": user.role.value}

    @api.get("/test/staff")
    async def staff_endpoint(
        user: Annotated[User, Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER))],
    ) -> dict[str, str]:
        return {"role": user.role.value}

    return api


@pytest_asyncio.fixture
async def client(test_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=test_app, client=(str(IPv6Address(uuid4().int)), 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
