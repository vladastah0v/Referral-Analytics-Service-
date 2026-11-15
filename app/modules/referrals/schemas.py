from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class ReferralCodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_at: AwareDatetime | None = None
    max_uses: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)] | None = None

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            value = value.astimezone(UTC)
            if value <= datetime.now(UTC):
                raise ValueError("Expiration must be in the future")
        return value


class ReferralCodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID
    code: str
    is_active: bool
    created_at: datetime
    expires_at: datetime | None
    max_uses: int | None


class ReferralCodePage(BaseModel):
    items: list[ReferralCodeRead]
    limit: int
    offset: int
