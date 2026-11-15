from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    CONVERTED = "converted"
    LOST = "lost"


def _lead_status_enum(name: str) -> Enum:
    return Enum(
        LeadStatus,
        values_callable=lambda enum: [member.value for member in enum],
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["referral_attribution_id", "user_id"],
            ["referral_attributions.id", "referral_attributions.referred_user_id"],
            name="fk_leads_attribution_user",
            ondelete="RESTRICT",
        ),
        CheckConstraint("source ~ '[^[:space:]]'", name="ck_leads_source_not_blank"),
        UniqueConstraint("user_id", "creation_request_id", name="uq_leads_user_creation_request"),
        CheckConstraint(
            "(creation_request_id IS NULL AND creation_request_hash IS NULL) OR "
            "(creation_request_id IS NOT NULL AND creation_request_hash IS NOT NULL "
            "AND creation_request_hash ~ '^[0-9a-f]{64}$')",
            name="ck_leads_creation_request_pair",
        ),
        Index("ix_leads_created_id", "created_at", "id"),
        Index("ix_leads_user_created", "user_id", "created_at", "id"),
        Index("ix_leads_status_created", "status", "created_at", "id"),
        Index("ix_leads_manager_created", "assigned_manager_id", "created_at", "id"),
        Index("ix_leads_attribution_id", "referral_attribution_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    referral_attribution_id: Mapped[UUID | None] = mapped_column(nullable=True)
    status: Mapped[LeadStatus] = mapped_column(
        _lead_status_enum("lead_status"),
        nullable=False,
        default=LeadStatus.NEW,
        server_default=text("'new'"),
    )
    assigned_manager_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    creation_request_id: Mapped[UUID | None] = mapped_column(nullable=True)
    creation_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LeadStatusHistory(Base):
    __tablename__ = "lead_status_history"
    __table_args__ = (
        CheckConstraint(
            "previous_status IS NULL OR previous_status <> new_status",
            name="ck_lead_status_history_status_changed",
        ),
        Index("ix_lead_status_history_lead_changed_id", "lead_id", "changed_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    lead_id: Mapped[UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="RESTRICT"), nullable=False
    )
    previous_status: Mapped[LeadStatus | None] = mapped_column(
        _lead_status_enum("lead_history_previous_status"), nullable=True
    )
    new_status: Mapped[LeadStatus] = mapped_column(
        _lead_status_enum("lead_history_new_status"), nullable=False
    )
    changed_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
