"""connector: time of the last voucher change scan (throttles scans on large Tally companies)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tally_connectors", sa.Column("voucher_scan_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tally_connectors", "voucher_scan_at")
