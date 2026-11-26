"""voucher type name-wise rights of a user in a company (view / create / alter / delete)

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_company_access", sa.Column("voucher_rights", sa.JSON()))


def downgrade() -> None:
    op.drop_column("user_company_access", "voucher_rights")
