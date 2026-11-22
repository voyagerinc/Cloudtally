"""reports a licence allows (empty: every report)

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-22
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("licenses", sa.Column("reports", sa.JSON()))


def downgrade() -> None:
    op.drop_column("licenses", "reports")
