"""Push and pull: automatic pull / push per company, last push / pull, push / pull history

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tally_connectors", sa.Column("auto_pull", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("tally_connectors", sa.Column("auto_push", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("tally_connectors", sa.Column("last_pull_at", sa.DateTime(timezone=True)))
    op.add_column("tally_connectors", sa.Column("last_pull_by", sa.String(100)))
    op.add_column("tally_connectors", sa.Column("last_push_at", sa.DateTime(timezone=True)))
    op.add_column("tally_connectors", sa.Column("last_push_by", sa.String(100)))
    # what the software already knew: the last time something was read from / written to Tally
    op.execute("UPDATE tally_connectors SET last_pull_at = last_inbound_at, last_push_at = last_outbound_at")
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column("started_by", sa.String(100)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("counts", sa.JSON(), nullable=False),
    )
    op.create_index("ix_sync_runs_company_started", "sync_runs", ["company_id", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_sync_runs_company_started", "sync_runs")
    op.drop_table("sync_runs")
    for c in ("last_push_by", "last_push_at", "last_pull_by", "last_pull_at", "auto_push", "auto_pull"):
        op.drop_column("tally_connectors", c)
