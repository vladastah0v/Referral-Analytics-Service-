"""Create CRM leads and their status history.

Revision ID: 20261115_03
Revises: 20261115_02
"""

import sqlalchemy as sa
from alembic import op

revision = "20261115_03"
down_revision = "20261115_02"
branch_labels = None
depends_on = None


def _status_enum(name: str) -> sa.Enum:
    return sa.Enum(
        "new",
        "contacted",
        "qualified",
        "converted",
        "lost",
        name=name,
        native_enum=False,
        create_constraint=True,
    )


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_referral_attributions_id_user", "referral_attributions", ["id", "referred_user_id"]
    )
    op.create_table(
        "leads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("referral_attribution_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status", _status_enum("lead_status"), server_default=sa.text("'new'"), nullable=False
        ),
        sa.Column("assigned_manager_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("source ~ '[^[:space:]]'", name="ck_leads_source_not_blank"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assigned_manager_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["referral_attribution_id", "user_id"],
            ["referral_attributions.id", "referral_attributions.referred_user_id"],
            name="fk_leads_attribution_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_leads_created_id", "leads", ["created_at", "id"])
    op.create_index("ix_leads_user_created", "leads", ["user_id", "created_at", "id"])
    op.create_index("ix_leads_status_created", "leads", ["status", "created_at", "id"])
    op.create_index(
        "ix_leads_manager_created", "leads", ["assigned_manager_id", "created_at", "id"]
    )
    op.create_index("ix_leads_attribution_id", "leads", ["referral_attribution_id"])
    op.create_table(
        "lead_status_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("previous_status", _status_enum("lead_history_previous_status"), nullable=True),
        sa.Column("new_status", _status_enum("lead_history_new_status"), nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=False),
        sa.Column(
            "changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "previous_status IS NULL OR previous_status <> new_status",
            name="ck_lead_status_history_status_changed",
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_lead_status_history_lead_changed_id",
        "lead_status_history",
        ["lead_id", "changed_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_lead_status_history_lead_changed_id", table_name="lead_status_history")
    op.drop_table("lead_status_history")
    op.drop_index("ix_leads_attribution_id", table_name="leads")
    op.drop_index("ix_leads_manager_created", table_name="leads")
    op.drop_index("ix_leads_status_created", table_name="leads")
    op.drop_index("ix_leads_user_created", table_name="leads")
    op.drop_index("ix_leads_created_id", table_name="leads")
    op.drop_table("leads")
    op.drop_constraint("uq_referral_attributions_id_user", "referral_attributions", type_="unique")
