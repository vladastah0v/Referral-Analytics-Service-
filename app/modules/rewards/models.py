from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.modules.referrals.models import RewardType


class RewardStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    CANCELLED = "cancelled"
    PAID = "paid"


def reward_status_type(name: str):
    return Enum(
        RewardStatus,
        values_callable=lambda enum: [item.value for item in enum],
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


class ReferralReward(Base):
    __tablename__ = "referral_rewards"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_referral_rewards_order"),
        ForeignKeyConstraint(
            ["order_id", "referral_attribution_id", "currency"],
            ["orders.id", "orders.referral_attribution_id", "orders.currency"],
            name="fk_referral_rewards_order_attribution_currency",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["referral_attribution_id", "referrer_id"],
            ["referral_attributions.id", "referral_attributions.referrer_id"],
            name="fk_referral_rewards_attribution_referrer",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "reward_amount >= 0 AND reward_amount <> 'NaN'::numeric",
            name="ck_referral_rewards_amount",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_referral_rewards_currency"),
        CheckConstraint(
            "reward_value_snapshot > 0 AND reward_value_snapshot <> 'NaN'::numeric "
            "AND (reward_type_snapshot <> 'percentage' OR reward_value_snapshot <= 100)",
            name="ck_referral_rewards_rule_value",
        ),
        CheckConstraint(
            "max_reward_snapshot IS NULL OR "
            "(max_reward_snapshot > 0 AND max_reward_snapshot <> 'NaN'::numeric)",
            name="ck_referral_rewards_rule_max",
        ),
        CheckConstraint(
            "max_reward_snapshot IS NULL OR reward_amount <= max_reward_snapshot",
            name="ck_referral_rewards_amount_cap",
        ),
        CheckConstraint("rounding_mode = 'ROUND_HALF_UP'", name="ck_referral_rewards_rounding"),
        CheckConstraint(
            "status NOT IN ('approved', 'paid') OR approved_at IS NOT NULL",
            name="ck_referral_rewards_approved_at",
        ),
        CheckConstraint(
            "approved_at IS NULL OR approved_at >= created_at",
            name="ck_referral_rewards_approval_time",
        ),
        Index("ix_referral_rewards_referrer_created", "referrer_id", "created_at", "id"),
        Index("ix_referral_rewards_status_created", "status", "created_at", "id"),
        Index("ix_referral_rewards_program", "referral_program_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    referral_program_id: Mapped[UUID] = mapped_column(
        ForeignKey("referral_programs.id", ondelete="RESTRICT"), nullable=False
    )
    referral_attribution_id: Mapped[UUID] = mapped_column(nullable=False)
    order_id: Mapped[UUID] = mapped_column(nullable=False)
    referrer_id: Mapped[UUID] = mapped_column(nullable=False)
    reward_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[RewardStatus] = mapped_column(
        reward_status_type("referral_reward_status"),
        nullable=False,
        default=RewardStatus.PENDING,
        server_default=text("'pending'"),
    )
    reward_type_snapshot: Mapped[RewardType] = mapped_column(
        Enum(
            RewardType,
            values_callable=lambda enum: [item.value for item in enum],
            name="reward_snapshot_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    reward_value_snapshot: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    max_reward_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    rounding_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'ROUND_HALF_UP'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReferralRewardHistory(Base):
    __tablename__ = "referral_reward_history"
    __table_args__ = (
        UniqueConstraint("reward_id", "revision", name="uq_referral_reward_history_revision"),
        CheckConstraint("revision > 0", name="ck_referral_reward_history_revision"),
        CheckConstraint(
            "reward_amount >= 0 AND reward_amount <> 'NaN'::numeric",
            name="ck_referral_reward_history_amount",
        ),
        CheckConstraint("reason ~ '[^[:space:]]'", name="ck_referral_reward_history_reason"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    reward_id: Mapped[UUID] = mapped_column(
        ForeignKey("referral_rewards.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reward_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[RewardStatus] = mapped_column(
        reward_status_type("referral_reward_history_status"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
