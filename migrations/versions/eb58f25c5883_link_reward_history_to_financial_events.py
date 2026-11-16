"""link reward history to financial events

Revision ID: eb58f25c5883
Revises: d7953f7bb948
Create Date: 2026-11-16 18:51:02.633094
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "eb58f25c5883"
down_revision: str | None = "d7953f7bb948"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "referral_reward_history", sa.Column("financial_event_id", sa.Uuid(), nullable=True)
    )
    op.create_unique_constraint(
        "uq_reward_history_financial_event",
        "referral_reward_history",
        ["reward_id", "financial_event_id"],
    )
    op.create_foreign_key(
        "fk_reward_history_financial_event",
        "referral_reward_history",
        "order_financial_events",
        ["financial_event_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_reward_history_financial_event", "referral_reward_history", type_="foreignkey"
    )
    op.drop_constraint(
        "uq_reward_history_financial_event", "referral_reward_history", type_="unique"
    )
    op.drop_column("referral_reward_history", "financial_event_id")
