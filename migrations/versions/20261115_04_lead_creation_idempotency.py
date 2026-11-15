"""Add optional per-user idempotency keys for lead creation.

Revision ID: 20261115_04
Revises: 20261115_03
"""

import sqlalchemy as sa
from alembic import op

revision = "20261115_04"
down_revision = "20261115_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("creation_request_id", sa.Uuid(), nullable=True))
    op.add_column("leads", sa.Column("creation_request_hash", sa.String(length=64), nullable=True))
    op.create_unique_constraint(
        "uq_leads_user_creation_request", "leads", ["user_id", "creation_request_id"]
    )
    op.create_check_constraint(
        "ck_leads_creation_request_pair",
        "leads",
        "(creation_request_id IS NULL AND creation_request_hash IS NULL) OR "
        "(creation_request_id IS NOT NULL AND creation_request_hash IS NOT NULL "
        "AND creation_request_hash ~ '^[0-9a-f]{64}$')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_leads_creation_request_pair", "leads", type_="check")
    op.drop_constraint("uq_leads_user_creation_request", "leads", type_="unique")
    op.drop_column("leads", "creation_request_hash")
    op.drop_column("leads", "creation_request_id")
