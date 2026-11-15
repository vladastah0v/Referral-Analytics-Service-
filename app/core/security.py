import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import jwt
from anyio import CapacityLimiter, to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings

JWT_ISSUER = "referral-analytics-service"
JWT_AUDIENCE = "referral-analytics-api"
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=2,p=2$nRi7RuaIXQPbfohrg7tKuw$"
    "ecsxcF5wkCTNTzbwwJtRddL0Ka4kwCwz9SFUYoWR0Iw"
)
_password_hasher = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)
_password_limiter = CapacityLimiter(4)


class InvalidTokenError(Exception):
    pass


@dataclass(frozen=True)
class TokenClaims:
    user_id: UUID
    session_id: UUID


async def hash_password(password: str) -> str:
    return await to_thread.run_sync(_password_hasher.hash, password, limiter=_password_limiter)


async def verify_password(password_hash: str, password: str) -> bool:
    def verify() -> bool:
        try:
            return _password_hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    return await to_thread.run_sync(verify, limiter=_password_limiter)


def password_needs_rehash(password_hash: str) -> bool:
    return _password_hasher.check_needs_rehash(password_hash)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_token(
    user_id: UUID, session_id: UUID, token_type: Literal["access", "refresh"]
) -> tuple[str, datetime]:
    settings = get_settings()
    now = datetime.now(UTC)
    lifetime = (
        timedelta(minutes=settings.jwt_access_ttl_minutes)
        if token_type == "access"
        else timedelta(days=settings.jwt_refresh_ttl_days)
    )
    issued_at = int(now.timestamp())
    expires_at = int((now + lifetime).timestamp())
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "typ": token_type,
        "iat": issued_at,
        "exp": expires_at,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }
    if token_type == "refresh":
        payload["jti"] = secrets.token_urlsafe(32)
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    return token, datetime.fromtimestamp(expires_at, UTC)


def decode_token(token: str, expected_type: Literal["access", "refresh"]) -> TokenClaims:
    if len(token) > 4096:
        raise InvalidTokenError
    required = ["sub", "sid", "typ", "iat", "exp", "iss", "aud"]
    if expected_type == "refresh":
        required.append("jti")
    try:
        payload = jwt.decode(
            token,
            get_settings().jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
            options={"require": required},
        )
        if payload["typ"] != expected_type:
            raise InvalidTokenError
        if expected_type == "refresh" and (
            not isinstance(payload["jti"], str) or not payload["jti"]
        ):
            raise InvalidTokenError
        return TokenClaims(user_id=UUID(payload["sub"]), session_id=UUID(payload["sid"]))
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise InvalidTokenError from exc
