from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from pydantic import ValidationError

from app.core.config import DEVELOPMENT_JWT_SECRET, Settings, get_settings
from app.core.security import (
    JWT_AUDIENCE,
    JWT_ISSUER,
    InvalidTokenError,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)


async def test_password_hashing_and_verification() -> None:
    first = await hash_password("test-password-with-length")
    second = await hash_password("test-password-with-length")
    assert first != second
    assert first.startswith("$argon2id$")
    assert await verify_password(first, "test-password-with-length")
    assert not await verify_password(first, "wrong-password")
    assert not await verify_password("invalid-hash", "wrong-password")


def test_access_and_refresh_tokens_are_distinct_types() -> None:
    user_id, session_id = uuid4(), uuid4()
    access, _ = create_token(user_id, session_id, "access")
    refresh, _ = create_token(user_id, session_id, "refresh")
    assert decode_token(access, "access").user_id == user_id
    assert decode_token(refresh, "refresh").session_id == session_id
    with pytest.raises(InvalidTokenError):
        decode_token(refresh, "access")
    with pytest.raises(InvalidTokenError):
        decode_token(access, "refresh")
    with pytest.raises(InvalidTokenError):
        decode_token("x" * 4097, "access")
    with pytest.raises(InvalidTokenError):
        decode_token("malformed", "access")


def test_token_validation_checks_expiry_audience_and_signature() -> None:
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid4()),
        "sid": str(uuid4()),
        "typ": "access",
        "iat": now,
        "exp": now - timedelta(seconds=1),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }
    secret = get_settings().jwt_secret.get_secret_value()
    with pytest.raises(InvalidTokenError):
        decode_token(jwt.encode(payload, secret, algorithm="HS256"), "access")
    payload["exp"] = now + timedelta(minutes=5)
    payload["aud"] = "another-service"
    with pytest.raises(InvalidTokenError):
        decode_token(jwt.encode(payload, secret, algorithm="HS256"), "access")
    payload["aud"] = JWT_AUDIENCE
    with pytest.raises(InvalidTokenError):
        decode_token(
            jwt.encode(payload, "another-secret-longer-than-32-bytes", algorithm="HS256"), "access"
        )
    del payload["exp"]
    with pytest.raises(InvalidTokenError):
        decode_token(jwt.encode(payload, secret, algorithm="HS256"), "access")


def test_production_rejects_default_or_short_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production", jwt_secret=DEVELOPMENT_JWT_SECRET)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production", jwt_secret="short")
    settings = Settings(
        _env_file=None,
        app_env="production",
        jwt_secret="x" * 48,
        database_url="postgresql+asyncpg://referral:configured-password@db/referral",
    )
    assert settings.app_env == "production"
    assert "x" * 48 not in repr(settings)
