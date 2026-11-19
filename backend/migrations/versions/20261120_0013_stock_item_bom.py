"""stock item BOM (Tally's bill of materials / component list)

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stock_items", sa.Column("bom", sa.JSON()))


def downgrade() -> None:
    op.drop_column("stock_items", "bom")
