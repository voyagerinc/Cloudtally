"""connector: changed vouchers still waiting to be read from Tally

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-01
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tally_connectors", sa.Column("voucher_backlog", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("tally_connectors", "voucher_backlog")
