"""when the full voucher list was last asked from Tally to find vouchers deleted there

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tally_connectors", sa.Column("deleted_check_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("tally_connectors", "deleted_check_at")
