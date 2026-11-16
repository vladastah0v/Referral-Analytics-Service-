from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.modules.referrals.models import RewardType
from app.modules.rewards.models import RewardStatus


class RewardRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    referral_program_id: UUID
    reward_amount: Decimal
    currency: str
    status: RewardStatus
    created_at: datetime
    approved_at: datetime | None
    revision: int

    @field_serializer("reward_amount")
    def serialize_amount(self, value: Decimal) -> str:
        return format(value, ".2f")


class AdminRewardRead(RewardRead):
    order_id: UUID
    referral_attribution_id: UUID
    referrer_id: UUID
    reward_type_snapshot: RewardType
    reward_value_snapshot: Decimal
    max_reward_snapshot: Decimal | None
    rounding_mode: str


class RewardPage(BaseModel):
    items: list[RewardRead]
    limit: int
    offset: int


class AdminRewardPage(BaseModel):
    items: list[AdminRewardRead]
    limit: int
    offset: int


class RewardApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, le=2147483647, strict=True)
