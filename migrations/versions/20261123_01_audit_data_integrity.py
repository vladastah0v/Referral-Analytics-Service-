"""Backfill legacy order dates and version failed snapshot requests."""

import sqlalchemy as sa
from alembic import op

revision = "20261123_01"
down_revision = "20261118_04"
branch_labels = None
depends_on = None

SNAPSHOT_INPUTS = (
    "referral_clicks",
    "referral_attributions",
    "leads",
    "order_financial_events",
    "referral_reward_history",
)


def upgrade() -> None:
    # Older revisions never recorded the source timestamp. Use the earliest recorded evidence.
    op.execute("""
        UPDATE orders o SET external_created_at = LEAST(
            o.created_at, o.paid_at, o.cancelled_at,
            (SELECT min(f.occurred_at) FROM order_financial_events f WHERE f.order_id = o.id)
        )
        WHERE o.external_created_at IS NULL
    """)
    op.add_column("analytics_snapshot_failures", sa.Column("request_id", sa.BigInteger()))
    op.add_column("analytics_snapshot_failures", sa.Column("request_count", sa.BigInteger()))
    op.execute("""
        CREATE FUNCTION snapshot_inputs_truncated() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO analytics_snapshot_requests(day)
            SELECT day FROM analytics_daily_snapshots;
            RETURN NULL;
        END
        $$
    """)
    for table in SNAPSHOT_INPUTS:
        op.execute(
            f"CREATE TRIGGER snapshot_truncate AFTER TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION snapshot_inputs_truncated()"
        )


def downgrade() -> None:
    for table in SNAPSHOT_INPUTS:
        op.execute(f"DROP TRIGGER snapshot_truncate ON {table}")
    op.execute("DROP FUNCTION snapshot_inputs_truncated()")
    op.drop_column("analytics_snapshot_failures", "request_count")
    op.drop_column("analytics_snapshot_failures", "request_id")
