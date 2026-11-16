"""create referral rewards and history

Revision ID: d7953f7bb948
Revises: b99bb4d96baa
Create Date: 2026-11-16 18:45:00.797360
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7953f7bb948"
down_revision: str | None = "b99bb4d96baa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_orders_id_attribution_currency", "orders", ["id", "referral_attribution_id", "currency"]
    )
    op.create_unique_constraint(
        "uq_referral_attributions_id_referrer", "referral_attributions", ["id", "referrer_id"]
    )
    op.create_table(
        "referral_rewards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("referral_program_id", sa.Uuid(), nullable=False),
        sa.Column("referral_attribution_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("referrer_id", sa.Uuid(), nullable=False),
        sa.Column("reward_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "approved",
                "cancelled",
                "paid",
                name="referral_reward_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "reward_type_snapshot",
            sa.Enum(
                "fixed",
                "percentage",
                name="reward_snapshot_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("reward_value_snapshot", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("max_reward_snapshot", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column(
            "rounding_mode",
            sa.String(length=32),
            server_default=sa.text("'ROUND_HALF_UP'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_referral_rewards_currency"),
        sa.CheckConstraint(
            "max_reward_snapshot IS NULL OR "
            "(max_reward_snapshot > 0 AND max_reward_snapshot <> 'NaN'::numeric)",
            name="ck_referral_rewards_rule_max",
        ),
        sa.CheckConstraint(
            "reward_amount >= 0 AND reward_amount <> 'NaN'::numeric",
            name="ck_referral_rewards_amount",
        ),
        sa.CheckConstraint(
            "reward_value_snapshot > 0 AND reward_value_snapshot <> 'NaN'::numeric "
            "AND (reward_type_snapshot <> 'percentage' OR reward_value_snapshot <= 100)",
            name="ck_referral_rewards_rule_value",
        ),
        sa.CheckConstraint("rounding_mode = 'ROUND_HALF_UP'", name="ck_referral_rewards_rounding"),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'paid') OR approved_at IS NOT NULL",
            name="ck_referral_rewards_approved_at",
        ),
        sa.CheckConstraint(
            "approved_at IS NULL OR approved_at >= created_at",
            name="ck_referral_rewards_approval_time",
        ),
        sa.CheckConstraint(
            "max_reward_snapshot IS NULL OR reward_amount <= max_reward_snapshot",
            name="ck_referral_rewards_amount_cap",
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "referral_attribution_id", "currency"],
            ["orders.id", "orders.referral_attribution_id", "orders.currency"],
            name="fk_referral_rewards_order_attribution_currency",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["referral_attribution_id", "referrer_id"],
            ["referral_attributions.id", "referral_attributions.referrer_id"],
            name="fk_referral_rewards_attribution_referrer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["referral_program_id"], ["referral_programs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", name="uq_referral_rewards_order"),
    )
    op.create_index(
        "ix_referral_rewards_program", "referral_rewards", ["referral_program_id"], unique=False
    )
    op.create_index(
        "ix_referral_rewards_referrer_created",
        "referral_rewards",
        ["referrer_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_referral_rewards_status_created",
        "referral_rewards",
        ["status", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "referral_reward_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reward_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reward_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "approved",
                "cancelled",
                "paid",
                name="referral_reward_history_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("reason ~ '[^[:space:]]'", name="ck_referral_reward_history_reason"),
        sa.CheckConstraint(
            "reward_amount >= 0 AND reward_amount <> 'NaN'::numeric",
            name="ck_referral_reward_history_amount",
        ),
        sa.CheckConstraint("revision > 0", name="ck_referral_reward_history_revision"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reward_id"], ["referral_rewards.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reward_id", "revision", name="uq_referral_reward_history_revision"),
    )


def downgrade() -> None:
    op.drop_table("referral_reward_history")
    op.drop_index("ix_referral_rewards_status_created", table_name="referral_rewards")
    op.drop_index("ix_referral_rewards_referrer_created", table_name="referral_rewards")
    op.drop_index("ix_referral_rewards_program", table_name="referral_rewards")
    op.drop_table("referral_rewards")
    op.drop_constraint(
        "uq_referral_attributions_id_referrer", "referral_attributions", type_="unique"
    )
    op.drop_constraint("uq_orders_id_attribution_currency", "orders", type_="unique")
