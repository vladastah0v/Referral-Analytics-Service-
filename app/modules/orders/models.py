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
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OrderStatus(StrEnum):
    CREATED = "created"
    PAID = "paid"
    CANCELLED = "cancelled"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"


class FinancialEventType(StrEnum):
    PAYMENT = "payment"
    REFUND = "refund"
    ADJUSTMENT_CREDIT = "adjustment_credit"
    ADJUSTMENT_DEBIT = "adjustment_debit"


def _enum_values(enum):
    return [member.value for member in enum]


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("source_system", "external_order_id", name="uq_orders_source_external_id"),
        UniqueConstraint("id", "currency", name="uq_orders_id_currency"),
        UniqueConstraint(
            "id", "referral_attribution_id", "currency", name="uq_orders_id_attribution_currency"
        ),
        ForeignKeyConstraint(
            ["referral_attribution_id", "user_id"],
            ["referral_attributions.id", "referral_attributions.referred_user_id"],
            name="fk_orders_attribution_user",
            ondelete="RESTRICT",
        ),
        CheckConstraint("source_system ~ '[^[:space:]]'", name="ck_orders_source_not_blank"),
        CheckConstraint(
            "external_order_id ~ '[^[:space:]]'", name="ck_orders_external_id_not_blank"
        ),
        CheckConstraint(
            "total_amount >= 0 AND total_amount <> 'NaN'::numeric", name="ck_orders_total_amount"
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_orders_currency"),
        CheckConstraint(
            "referral_attribution_id IS NULL OR attribution_frozen_at IS NOT NULL",
            name="ck_orders_attribution_frozen",
        ),
        CheckConstraint(
            "status NOT IN ('paid', 'partially_refunded', 'refunded') OR paid_at IS NOT NULL",
            name="ck_orders_paid_at_required",
        ),
        Index("ix_orders_user_created", "user_id", "created_at", "id"),
        Index("ix_orders_status_paid_at", "status", "paid_at"),
        Index("ix_orders_attribution_paid_at", "referral_attribution_id", "paid_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    external_order_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_system: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    referral_attribution_id: Mapped[UUID | None] = mapped_column(nullable=True)
    attribution_frozen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[OrderStatus] = mapped_column(
        Enum(
            OrderStatus,
            values_callable=_enum_values,
            name="order_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        default=OrderStatus.CREATED,
        server_default=text("'created'"),
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OrderFinancialEvent(Base):
    __tablename__ = "order_financial_events"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "external_financial_event_id",
            name="uq_order_financial_events_source_external_id",
        ),
        ForeignKeyConstraint(
            ["order_id", "currency"],
            ["orders.id", "orders.currency"],
            name="fk_order_financial_events_order_currency",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "source_system ~ '[^[:space:]]'", name="ck_order_financial_events_source_not_blank"
        ),
        CheckConstraint(
            "external_financial_event_id ~ '[^[:space:]]'",
            name="ck_order_financial_events_external_id_not_blank",
        ),
        CheckConstraint(
            "amount > 0 AND amount <> 'NaN'::numeric", name="ck_order_financial_events_amount"
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_order_financial_events_currency"),
        Index("ix_order_financial_events_order_occurred", "order_id", "occurred_at", "id"),
        Index("ix_order_financial_events_currency_occurred", "currency", "occurred_at"),
        Index(
            "uq_order_financial_events_one_payment",
            "order_id",
            unique=True,
            postgresql_where=text("event_type = 'payment'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(nullable=False)
    source_system: Mapped[str] = mapped_column(String(128), nullable=False)
    external_financial_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[FinancialEventType] = mapped_column(
        Enum(
            FinancialEventType,
            values_callable=_enum_values,
            name="financial_event_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
