"""vouchers per Tally request, chosen per company on the Tally connector (empty: automatic)

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-24
"""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tally_connectors", sa.Column("voucher_batch_size", sa.Integer()))


def downgrade() -> None:
    op.drop_column("tally_connectors", "voucher_batch_size")
