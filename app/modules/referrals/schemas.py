from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated
from uuid import UUID

from pydantic import (
    UUID4,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.modules.referrals.models import RewardType


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


class ReferralCodeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: StrictBool | None = None
    expires_at: AwareDatetime | None = None
    max_uses: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)] | None = None

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        return ReferralCodeCreate.validate_expiry(value)

    @model_validator(mode="after")
    def validate_update(self):
        if not self.model_fields_set:
            raise ValueError("At least one setting must be provided")
        if "is_active" in self.model_fields_set and self.is_active is not False:
            raise ValueError("Only deactivation is permitted")
        return self


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


class ReferralProgramCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    reward_type: RewardType
    reward_value: Annotated[
        Decimal, Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)
    ]
    max_reward: (
        Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)] | None
    ) = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    is_active: StrictBool = True
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def normalize_dates(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def validate_program(self):
        if self.reward_type == RewardType.PERCENTAGE and self.reward_value > 100:
            raise ValueError("Percentage cannot exceed 100")
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("End date must be after start date")
        return self


class ReferralProgramUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
        | None
    ) = None
    reward_type: RewardType | None = None
    reward_value: (
        Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)] | None
    ) = None
    max_reward: (
        Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)] | None
    ) = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    is_active: StrictBool | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def normalize_dates(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def validate_fields(self):
        if not self.model_fields_set:
            raise ValueError("At least one setting must be provided")
        for name in ("name", "reward_type", "reward_value", "currency", "is_active", "starts_at"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class ReferralProgramRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    reward_type: RewardType
    reward_value: Decimal
    max_reward: Decimal | None
    currency: str
    is_active: bool
    starts_at: datetime
    ends_at: datetime | None


class ReferralProgramPage(BaseModel):
    items: list[ReferralProgramRead]
    limit: int
    offset: int


class ReferralClickCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    visitor_id: UUID4
    request_id: UUID4
    source: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
        | None
    ) = None
    campaign: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
        | None
    ) = None


class ReferralClickRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    referral_code_id: UUID
    request_id: UUID
    clicked_at: datetime
    source: str | None
    campaign: str | None


class ReferralMetrics(BaseModel):
    total_clicks: int
    unique_visitors: int
    referred_registrations: int
    conversion_rate: Decimal | None

    @classmethod
    def from_counts(cls, clicks: int, visitors: int, registrations: int) -> "ReferralMetrics":
        rate = (
            (Decimal(registrations) * 100 / visitors).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if visitors
            else None
        )
        return cls(
            total_clicks=clicks,
            unique_visitors=visitors,
            referred_registrations=registrations,
            conversion_rate=rate,
        )


class ReferralStatistics(ReferralMetrics):
    date_from: datetime | None
    date_to: datetime | None
