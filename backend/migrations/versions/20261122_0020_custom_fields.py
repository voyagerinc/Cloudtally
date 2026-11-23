"""custom (TDL) fields from Tally: kept on vouchers; the fields a connector asks Tally for

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-22
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vouchers", sa.Column("custom_fields", sa.JSON()))
    op.add_column("tally_connectors", sa.Column("custom_fields", sa.JSON()))


def downgrade() -> None:
    op.drop_column("tally_connectors", "custom_fields")
    op.drop_column("vouchers", "custom_fields")
