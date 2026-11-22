"""two-way Tally sync becomes a licensed module: licences that exist keep it (they synced both ways before)

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-22
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None
MODULE = "tally_two_way"


def _change(add: bool) -> None:
    conn = op.get_bind()
    for lid, mods in conn.execute(sa.text("SELECT id, modules FROM licenses")).all():
        mods = json.loads(mods) if isinstance(mods, str) else list(mods or [])
        if add and "tally_sync" not in mods:
            continue                         # no Tally sync at all: nothing to keep
        new = sorted(set(mods) | {MODULE}) if add else [m for m in mods if m != MODULE]
        if new != mods:
            conn.execute(sa.text("UPDATE licenses SET modules = CAST(:m AS JSON) WHERE id = :i"),
                         {"m": json.dumps(new), "i": lid})


def upgrade() -> None:
    _change(True)


def downgrade() -> None:
    _change(False)
