"""users: password sign-in, lockout, sessions, and custom per-company permissions

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("users", "api_key_hash", existing_type=sa.String(64), nullable=True)
    op.add_column("users", sa.Column("password_hash", sa.String(200)))
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("failed_logins", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("created_by", sa.String(100)))
    op.add_column("user_company_access", sa.Column("permissions", sa.JSON()))
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client", sa.String(200)),
    )


def downgrade() -> None:
    op.drop_table("user_sessions")
    op.drop_column("user_company_access", "permissions")
    for col in ("created_by", "locked_until", "failed_logins", "must_change_password", "password_hash"):
        op.drop_column("users", col)
    op.alter_column("users", "api_key_hash", existing_type=sa.String(64), nullable=False)
