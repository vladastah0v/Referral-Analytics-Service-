import asyncio
from hashlib import sha256
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.redis import redis_client

WINDOW_SECONDS = 60
INCREMENT_WINDOW = """
local attempts = redis.call('INCR', KEYS[1])
if attempts == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {attempts, redis.call('TTL', KEYS[1])}
"""


def get_auth_rate_limit_redis() -> Redis:
    return redis_client


def limit_auth_requests(action: str, limit: int):
    async def enforce(
        request: Request,
        store: Annotated[Redis, Depends(get_auth_rate_limit_redis)],
    ) -> None:
        settings = get_settings()
        address = request.client.host if request.client else "unknown"
        bucket = sha256(address.encode()).hexdigest()
        key = f"{settings.analytics_cache_namespace}:auth-limit:{action}:{bucket}"
        try:
            async with asyncio.timeout(0.25):
                attempts, ttl = await store.eval(INCREMENT_WINDOW, 1, key, WINDOW_SECONDS)
        except (RedisError, OSError, TimeoutError) as exc:
            if settings.app_env == "production":
                raise HTTPException(503, "Authentication temporarily unavailable") from exc
            return
        if attempts > limit:
            raise HTTPException(429, "Too many requests", headers={"Retry-After": str(max(1, ttl))})

    return enforce
