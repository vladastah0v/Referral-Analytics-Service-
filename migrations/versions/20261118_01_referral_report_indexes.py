"""Add indexes for referral report time ranges and channel filters."""

from alembic import op

revision = "20261118_01"
down_revision = "eb58f25c5883"
branch_labels = None
depends_on = None

indexes = (
    ("ix_referral_clicks_clicked_at", "referral_clicks", ["clicked_at"]),
    ("ix_referral_clicks_campaign_time", "referral_clicks", ["campaign", "clicked_at"]),
    ("ix_referral_clicks_source_time", "referral_clicks", ["source", "clicked_at"]),
    ("ix_referral_attributions_attributed_at", "referral_attributions", ["attributed_at"]),
)


def upgrade() -> None:
    for name, table, columns in indexes:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _ in reversed(indexes):
        op.drop_index(name, table_name=table)
