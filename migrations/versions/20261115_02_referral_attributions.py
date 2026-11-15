"""Create referral attribution records with consistent source references.

Revision ID: 20261115_02
Revises: bb236618146b
"""

import sqlalchemy as sa
from alembic import op

revision = "20261115_02"
down_revision = "bb236618146b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_referral_codes_id_owner", "referral_codes", ["id", "owner_id"])
    op.create_unique_constraint(
        "uq_referral_clicks_id_code", "referral_clicks", ["id", "referral_code_id"]
    )
    op.create_table(
        "referral_attributions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("referral_code_id", sa.Uuid(), nullable=False),
        sa.Column("referrer_id", sa.Uuid(), nullable=False),
        sa.Column("referred_user_id", sa.Uuid(), nullable=False),
        sa.Column("click_id", sa.Uuid(), nullable=False),
        sa.Column(
            "attributed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "attribution_model",
            sa.Enum(
                "last_referral_click",
                name="referral_attribution_model",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.CheckConstraint(
            "referrer_id <> referred_user_id", name="ck_referral_attributions_not_self"
        ),
        sa.ForeignKeyConstraint(["referrer_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["referred_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["referral_code_id", "referrer_id"],
            ["referral_codes.id", "referral_codes.owner_id"],
            name="fk_referral_attributions_code_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["click_id", "referral_code_id"],
            ["referral_clicks.id", "referral_clicks.referral_code_id"],
            name="fk_referral_attributions_click_code",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("referred_user_id", name="uq_referral_attributions_referred_user"),
    )
    op.create_index(
        "ix_referral_attributions_code_id", "referral_attributions", ["referral_code_id"]
    )
    op.create_index("ix_referral_attributions_click_id", "referral_attributions", ["click_id"])
    op.create_index(
        "ix_referral_attributions_referrer_attributed",
        "referral_attributions",
        ["referrer_id", "attributed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_referral_attributions_referrer_attributed", table_name="referral_attributions"
    )
    op.drop_index("ix_referral_attributions_click_id", table_name="referral_attributions")
    op.drop_index("ix_referral_attributions_code_id", table_name="referral_attributions")
    op.drop_table("referral_attributions")
    op.drop_constraint("uq_referral_clicks_id_code", "referral_clicks", type_="unique")
    op.drop_constraint("uq_referral_codes_id_owner", "referral_codes", type_="unique")
