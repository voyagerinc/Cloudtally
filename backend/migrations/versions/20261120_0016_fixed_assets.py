"""fixed asset registers (the Fixed Assets page), one per company

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fixed_asset_states",
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("state", sa.JSON()),
        sa.Column("saved_by", sa.String(200)),
        sa.Column("saved_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("fixed_asset_states")
