"""voucher types: whether they affect ledger balances and stock; direction of value-only stock lines

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

ACCOUNTING = ("Contra", "Payment", "Receipt", "Journal", "Sales", "Purchase", "Credit Note", "Debit Note", "Payroll")
STOCK = ("Sales", "Purchase", "Credit Note", "Debit Note", "Journal", "Stock Journal", "Delivery Note",
         "Receipt Note", "Material In", "Material Out", "Rejections In", "Rejections Out")


def upgrade() -> None:
    op.add_column("voucher_types", sa.Column("affects_accounts", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("voucher_types", sa.Column("affects_stock", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("voucher_inventory_lines", sa.Column("inward", sa.Boolean(), nullable=True))
    vt = sa.table("voucher_types", sa.column("base_type", sa.String), sa.column("affects_accounts", sa.Boolean),
                  sa.column("affects_stock", sa.Boolean))
    op.execute(vt.update().values(affects_accounts=vt.c.base_type.in_(ACCOUNTING), affects_stock=vt.c.base_type.in_(STOCK)))


def downgrade() -> None:
    op.drop_column("voucher_inventory_lines", "inward")
    op.drop_column("voucher_types", "affects_stock")
    op.drop_column("voucher_types", "affects_accounts")
