"""masters: long texts from Tally (HSN descriptions, mailing names) are no longer refused

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-06
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

COLUMNS = [("stock_items", "hsn_description", 300), ("stock_groups", "hsn_description", 300),
           ("tax_codes", "description", 300), ("ledgers", "mailing_name", 300)]


def upgrade() -> None:
    for table, column, _ in COLUMNS:
        op.alter_column(table, column, type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    for table, column, size in COLUMNS:
        op.alter_column(table, column, type_=sa.String(size), existing_nullable=True,
                        postgresql_using=f"left({column}, {size})")
