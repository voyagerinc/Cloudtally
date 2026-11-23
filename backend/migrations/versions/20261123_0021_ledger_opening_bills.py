"""opening bill-wise details of party ledgers, from Tally (ageing includes the opening balance)

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ledgers", sa.Column("opening_bills", sa.JSON()))


def downgrade() -> None:
    op.drop_column("ledgers", "opening_bills")
