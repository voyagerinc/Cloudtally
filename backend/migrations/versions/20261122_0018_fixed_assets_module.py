"""fixed assets becomes a licensed module: licences that exist keep it (it was open to all before)

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-22
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def _change(add: bool) -> None:
    conn = op.get_bind()
    for lid, mods in conn.execute(sa.text("SELECT id, modules FROM licenses")).all():
        mods = json.loads(mods) if isinstance(mods, str) else list(mods or [])
        new = sorted(set(mods) | {"fixed_assets"}) if add else [m for m in mods if m != "fixed_assets"]
        if new != mods:
            conn.execute(sa.text("UPDATE licenses SET modules = CAST(:m AS JSON) WHERE id = :i"), {"m": json.dumps(new), "i": lid})


def upgrade() -> None:
    _change(True)


def downgrade() -> None:
    _change(False)
