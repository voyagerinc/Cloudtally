"""settings of the whole installation (e.g. allow Tally on a public IP)

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(60), primary_key=True),
        sa.Column("value", sa.JSON()),
        sa.Column("updated_by", sa.String(100)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
