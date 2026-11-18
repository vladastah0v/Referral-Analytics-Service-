"""Queue affected UTC days transactionally and store daily analytics snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261118_04"
down_revision = "20261118_03"
branch_labels = None
depends_on = None

# These are the time axes used by the unfiltered overview report.
FACT_DATES = {
    "referral_clicks": "clicked_at",
    "referral_attributions": "attributed_at",
    "leads": "created_at",
    "order_financial_events": "occurred_at",
    "referral_reward_history": "created_at",
}


def upgrade():
    op.create_table(
        "analytics_snapshot_requests",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
    )
    op.create_index("ix_snapshot_requests_day", "analytics_snapshot_requests", ["day"])
    op.create_table(
        "analytics_daily_snapshots",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("report", JSONB(), nullable=False),
        sa.Column(
            "refreshed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_table(
        "analytics_snapshot_failures",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("task_id", sa.String, nullable=False),
        sa.Column(
            "failed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    backfill = []
    for table, field in FACT_DATES.items():
        # Statement transition tables coalesce bulk writes to a single marker per day.
        for action, refs, query in (
            ("insert", "NEW TABLE AS fresh", "SELECT {day} FROM fresh"),
            ("delete", "OLD TABLE AS prior", "SELECT {day} FROM prior"),
            (
                "update",
                "NEW TABLE AS fresh OLD TABLE AS prior",
                "SELECT {day} FROM fresh UNION SELECT {day} FROM prior",
            ),
        ):
            name = f"snapshot_{table}_{action}"
            day = f"({field} AT TIME ZONE 'UTC')::date"
            dates = query.format(day=day)
            op.execute(f"""CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN INSERT INTO analytics_snapshot_requests(day)
                SELECT DISTINCT * FROM ({dates}) dates;
                RETURN NULL; END $$""")
            op.execute(
                f"CREATE TRIGGER {name} AFTER {action} ON {table} REFERENCING {refs} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION {name}()"
            )
        backfill.append(f"SELECT ({field} AT TIME ZONE 'UTC')::date FROM {table}")
    op.execute("INSERT INTO analytics_snapshot_requests(day) " + " UNION ".join(backfill))


def downgrade():
    for table in FACT_DATES:
        for action in ("insert", "delete", "update"):
            name = f"snapshot_{table}_{action}"
            op.execute(f"DROP TRIGGER {name} ON {table}")
            op.execute(f"DROP FUNCTION {name}()")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_table("analytics_snapshot_failures")
    op.drop_table("analytics_daily_snapshots")
    op.drop_table("analytics_snapshot_requests")
