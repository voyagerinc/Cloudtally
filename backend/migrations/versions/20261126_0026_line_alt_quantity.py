"""alternate-unit quantity of stock lines (e.g. 140 PCS = 144.2 KGS), filled in from Tally's text already received

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("voucher_inventory_lines", sa.Column("alt_quantity", sa.Numeric(20, 4)))
    op.add_column("voucher_inventory_lines", sa.Column("alt_unit", sa.String(40)))
    from app.services.sync import alt_units
    alt_units.backfill(op.get_bind())


def downgrade() -> None:
    op.drop_column("voucher_inventory_lines", "alt_unit")
    op.drop_column("voucher_inventory_lines", "alt_quantity")
