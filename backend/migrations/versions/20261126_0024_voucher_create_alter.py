"""voucher permissions: "create and alter" (voucher.write) becomes two ticks, create and alter

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-26
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def _swap(old: set, new: set) -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT user_id, company_id, permissions FROM user_company_access "
                                "WHERE permissions IS NOT NULL")).all()
    for user_id, company_id, perms in rows:
        perms = set(json.loads(perms) if isinstance(perms, str) else perms)
        if not perms & old:
            continue
        conn.execute(sa.text("UPDATE user_company_access SET permissions = :p WHERE user_id = :u AND company_id = :c"),
                     {"p": json.dumps(sorted((perms - old) | new)), "u": user_id, "c": company_id})


def upgrade() -> None:
    _swap({"voucher.write"}, {"voucher.create", "voucher.alter"})


def downgrade() -> None:
    _swap({"voucher.create", "voucher.alter"}, {"voucher.write"})
