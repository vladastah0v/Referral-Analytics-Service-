import asyncio
import hashlib
import json
from datetime import UTC, datetime
from functools import wraps
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text

from app.core.config import get_settings
from app.core.dependencies import SessionDependency
from app.core.redis import redis_client


async def report_snapshot(session: SessionDependency) -> None:
    # All report sections, access checks and the cache version share a database snapshot.
    await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})


def get_cache_client() -> Redis | None:
    return redis_client if get_settings().analytics_cache_ttl_seconds else None


CacheClient = Annotated[Redis | None, Depends(get_cache_client)]


def cached_report(model: type[BaseModel]):
    """Wrap a route whose role dependency runs before the endpoint, including on hits."""

    def decorate(endpoint):
        @wraps(endpoint)
        async def wrapped(*, session, user, cache, response, **filters):
            async def compute():
                return await endpoint(
                    session=session, user=user, cache=cache, response=response, **filters
                )

            response.headers["Cache-Control"] = "private, no-store"
            if cache is None:
                return await compute()

            version = (
                await session.execute(
                    text(
                        "SELECT current_database(), current_schema(), "
                        "pg_postmaster_start_time()::text, "
                        "COALESCE(sum(revision), 0)::text FROM analytics_cache_revisions"
                    )
                )
            ).one()
            normalized = {
                name: value.astimezone(UTC).isoformat() if isinstance(value, datetime) else value
                for name, value in filters.items()
            }
            material = json.dumps(
                [
                    get_settings().analytics_cache_namespace,
                    list(version),
                    endpoint.__name__,
                    str(user.id),
                    user.role.value,
                    normalized,
                ],
                sort_keys=True,
            )
            key = "analytics:v1:" + hashlib.sha256(material.encode()).hexdigest()
            try:
                async with asyncio.timeout(0.25):
                    payload = await cache.get(key)
            except (RedisError, TimeoutError, OSError):
                return await compute()
            if payload is not None:
                try:
                    return model.model_validate_json(payload)
                except ValidationError:
                    pass

            result = await compute()
            try:
                async with asyncio.timeout(0.25):
                    await cache.set(
                        key, result.model_dump_json(), ex=get_settings().analytics_cache_ttl_seconds
                    )
            except (RedisError, TimeoutError, OSError):
                pass
            return result

        return wrapped

    return decorate
