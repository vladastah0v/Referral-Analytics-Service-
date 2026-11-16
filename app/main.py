from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import engine
from app.core.exceptions import (
    AuthenticationError,
    EmailAlreadyRegisteredError,
    PermissionDeniedError,
)
from app.core.redis import redis_client
from app.modules.analytics.router import router as analytics_router
from app.modules.auth.router import router as auth_router
from app.modules.leads.router import router as leads_router
from app.modules.orders.router import router as orders_router
from app.modules.referrals.admin_router import router as referral_admin_router
from app.modules.referrals.router import router as referrals_router
from app.modules.rewards.router import router as rewards_router
from app.modules.tracking.router import router as events_router
from app.modules.users.router import router as users_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await redis_client.aclose()
    await engine.dispose()


app = FastAPI(title="Referral Analytics Service", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router, prefix="/api/v1")
app.include_router(orders_router, prefix="/api/v1")
app.include_router(rewards_router, prefix="/api/v1")
app.include_router(analytics_router, prefix="/api/v1")
app.include_router(events_router, prefix="/api/v1")
app.include_router(leads_router, prefix="/api/v1")
app.include_router(referrals_router, prefix="/api/v1")
app.include_router(referral_admin_router, prefix="/api/v1")
app.include_router(users_router, prefix="/api/v1")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Never reflect passwords, tokens or entire request bodies in validation responses.
    errors = [
        {"type": error["type"], "loc": error["loc"], "msg": error["msg"]} for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(AuthenticationError)
async def authentication_error_handler(request: Request, exc: AuthenticationError) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": "Invalid credentials or session"},
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.exception_handler(PermissionDeniedError)
async def permission_error_handler(request: Request, exc: PermissionDeniedError) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": "Insufficient permissions"})


@app.exception_handler(EmailAlreadyRegisteredError)
async def email_error_handler(request: Request, exc: EmailAlreadyRegisteredError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": "Email is already registered"})


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
