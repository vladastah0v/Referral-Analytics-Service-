from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import engine
from app.core.redis import redis_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await redis_client.aclose()
    await engine.dispose()


app = FastAPI(title="Referral Analytics Service", version="0.1.0", lifespan=lifespan)


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def readiness() -> dict[str, str]:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await redis_client.ping()
    except (SQLAlchemyError, RedisError, OSError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail="Dependencies unavailable") from exc
    return {"status": "ok"}
