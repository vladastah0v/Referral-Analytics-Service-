"""Version analytics results inside transactions that change report inputs."""

import sqlalchemy as sa
from alembic import op

revision = "20261118_03"
down_revision = "20261118_02"
branch_labels = None
depends_on = None

INPUT_TABLES = (
    "referral_clicks",
    "referral_codes",
    "referral_attributions",
    "leads",
    "orders",
    "order_financial_events",
    "referral_rewards",
    "referral_reward_history",
)


def upgrade() -> None:
    op.create_table(
        "analytics_cache_revisions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("revision", sa.BigInteger, nullable=False),
    )
    # Writers use a free counter, or allocate one when every counter is busy.
    # This avoids waiting for a shared revision row while holding business locks.
    op.execute("""
        CREATE FUNCTION advance_analytics_revision() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE analytics_cache_revisions SET revision = revision + 1
            WHERE id = (SELECT id FROM analytics_cache_revisions ORDER BY id
                        LIMIT 1 FOR UPDATE SKIP LOCKED);
            IF NOT FOUND THEN
                INSERT INTO analytics_cache_revisions(revision) VALUES (1);
            END IF;
            RETURN NULL;
        END
        $$
    """)
    for table in INPUT_TABLES:
        op.execute(
            f"CREATE TRIGGER analytics_revision AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE "
            f"ON {table} FOR EACH STATEMENT EXECUTE FUNCTION advance_analytics_revision()"
        )


def downgrade() -> None:
    for table in INPUT_TABLES:
        op.execute(f"DROP TRIGGER analytics_revision ON {table}")
    op.execute("DROP FUNCTION advance_analytics_revision()")
    op.drop_table("analytics_cache_revisions")
