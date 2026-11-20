"""licence keys kept encrypted, so an admin can see a key again

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("licenses", sa.Column("key_encrypted", sa.Text()))


def downgrade() -> None:
    op.drop_column("licenses", "key_encrypted")
