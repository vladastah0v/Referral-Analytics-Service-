"""Index reward revisions by recording time for period reports."""

from alembic import op

revision = "20261118_02"
down_revision = "20261118_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_reward_history_created_at", "referral_reward_history", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_reward_history_created_at", table_name="referral_reward_history")
