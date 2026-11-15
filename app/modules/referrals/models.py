from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RewardType(StrEnum):
    FIXED = "fixed"
    PERCENTAGE = "percentage"


class ReferralProgram(Base):
    __tablename__ = "referral_programs"
    __table_args__ = (
        CheckConstraint("reward_value > 0", name="ck_referral_programs_reward_positive"),
        CheckConstraint(
            "reward_type <> 'percentage' OR reward_value <= 100",
            name="ck_referral_programs_percentage_limit",
        ),
        CheckConstraint(
            "max_reward IS NULL OR max_reward > 0", name="ck_referral_programs_max_reward"
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_referral_programs_currency"),
        CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at", name="ck_referral_programs_dates"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    reward_type: Mapped[RewardType] = mapped_column(
        Enum(
            RewardType,
            values_callable=lambda enum: [member.value for member in enum],
            name="referral_reward_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    reward_value: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    max_reward: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReferralCode(Base):
    __tablename__ = "referral_codes"
    __table_args__ = (
        UniqueConstraint("code", name="uq_referral_codes_code"),
        CheckConstraint("max_uses IS NULL OR max_uses > 0", name="ck_referral_codes_max_uses"),
        Index("ix_referral_codes_owner_id", "owner_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ReferralClick(Base):
    __tablename__ = "referral_clicks"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_referral_clicks_request_id"),
        Index("ix_referral_clicks_visitor_clicked_id", "visitor_id", "clicked_at", "id"),
        Index("ix_referral_clicks_code_clicked_at", "referral_code_id", "clicked_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    referral_code_id: Mapped[UUID] = mapped_column(
        ForeignKey("referral_codes.id", ondelete="RESTRICT"), nullable=False
    )
    visitor_id: Mapped[UUID] = mapped_column(nullable=False)
    clicked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    campaign: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_id: Mapped[UUID] = mapped_column(nullable=False)
