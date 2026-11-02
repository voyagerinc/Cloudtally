"""ledgers: address, contact, bank and MSME details from Tally

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-01
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

COLUMNS = [
    ("pincode", sa.String(100)), ("country", sa.String(60)), ("place_of_supply", sa.String(100)),
    ("contact_person", sa.String(200)), ("phone", sa.String(100)), ("mobile", sa.String(100)), ("fax", sa.String(100)),
    ("email", sa.String(300)), ("email_cc", sa.String(300)), ("website", sa.String(200)),
    ("contacts", postgresql.JSONB()), ("bank_name", sa.String(200)), ("bank_branch", sa.String(300)),
    ("bank_account_holder", sa.String(200)), ("party_banks", postgresql.JSONB()),
    ("msme_type", sa.String(20)), ("msme_udyam_no", sa.String(40)), ("msme_activity", sa.String(40)),
    ("msme_from", sa.Date()),
]


def upgrade() -> None:
    for name, type_ in COLUMNS:
        op.add_column("ledgers", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_column("ledgers", name)
