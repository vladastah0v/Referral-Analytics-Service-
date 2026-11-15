from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEVELOPMENT_JWT_SECRET = "local-development-only-change-this-jwt-secret-before-deployment"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: str = (
        "postgresql+asyncpg://referral:local-development-only@localhost:5432/referral_analytics"
    )
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    jwt_secret: SecretStr = SecretStr(DEVELOPMENT_JWT_SECRET)
    jwt_access_ttl_minutes: int = Field(default=15, ge=1, le=1440)
    jwt_refresh_ttl_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="after")
    def validate_security_settings(self) -> Self:
        secret = self.jwt_secret.get_secret_value()
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 bytes")
        if self.app_env == "production" and secret == DEVELOPMENT_JWT_SECRET:
            raise ValueError("JWT_SECRET must be changed in production")
        if self.jwt_access_ttl_minutes * 60 > self.jwt_refresh_ttl_days * 86400:
            raise ValueError("Access token lifetime cannot exceed refresh token lifetime")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
