"""stock items: room for long stock group names in gst_source, and longer HSN codes

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-13
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

COLUMNS = [("stock_items", "gst_source", 250), ("stock_items", "hsn_code", 50), ("stock_items", "supply_type", 50),
           ("stock_items", "valuation_method", 50), ("stock_groups", "hsn_code", 50)]


def upgrade() -> None:
    for table, column, size in COLUMNS:
        op.alter_column(table, column, type_=sa.String(size), existing_nullable=True)


def downgrade() -> None:
    for table, column, _ in COLUMNS:
        op.alter_column(table, column, type_=sa.String(20), existing_nullable=True,
                        postgresql_using=f"left({column}, 20)")
