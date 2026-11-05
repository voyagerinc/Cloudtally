"""licensing: customers, licences, installations, releases, downloads, invites, client licence state

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-05
"""
import sqlalchemy as sa
from alembic import op

from app.core.db import install_db_extras

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _ts(name, **kw):
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(30), nullable=False, unique=True),
        sa.Column("legal_name", sa.String(300), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("contact_name", sa.String(200)),
        sa.Column("contact_email", sa.String(300)),
        sa.Column("contact_phone", sa.String(50)),
        sa.Column("gstin", sa.String(20)),
        sa.Column("notes", sa.Text()),
        _ts("created_at", nullable=False), _ts("updated_at", nullable=False),
    )
    op.create_table(
        "licenses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("customers.id"), nullable=False, index=True),
        sa.Column("product", sa.String(40), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("key_hint", sa.String(10), nullable=False),
        sa.Column("plan", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("starts_at", sa.Date(), nullable=False),
        sa.Column("expires_at", sa.Date()),
        sa.Column("max_users", sa.Integer(), nullable=False),
        sa.Column("max_installations", sa.Integer(), nullable=False),
        sa.Column("max_companies", sa.Integer()),
        sa.Column("modules", sa.JSON(), nullable=False),
        sa.Column("offline_grace_days", sa.Integer(), nullable=False),
        sa.Column("heartbeat_hours", sa.Integer(), nullable=False),
        sa.Column("min_version", sa.String(30)),
        sa.Column("max_version", sa.String(30)),
        sa.Column("created_by", sa.String(100)),
        sa.Column("notes", sa.Text()),
        _ts("created_at", nullable=False), _ts("updated_at", nullable=False),
    )
    op.create_table(
        "installations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("license_id", sa.Uuid(), sa.ForeignKey("licenses.id"), nullable=False, index=True),
        sa.Column("installation_id", sa.String(64), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("server_label", sa.String(200)),
        sa.Column("hostname", sa.String(200)),
        sa.Column("product_version", sa.String(30)),
        sa.Column("status", sa.String(20), nullable=False),
        _ts("activated_at", nullable=False), _ts("last_seen_at"),
        sa.Column("last_ip", sa.String(64)),
        _ts("deactivated_at"),
        sa.UniqueConstraint("license_id", "installation_id"),
    )
    op.create_table(
        "license_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("license_id", sa.Uuid(), index=True),
        sa.Column("customer_id", sa.Uuid(), index=True),
        sa.Column("installation_id", sa.String(64)),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("result", sa.String(20), nullable=False),
        sa.Column("actor", sa.String(100)),
        sa.Column("details", sa.JSON()),
        sa.Column("ip", sa.String(64)),
        _ts("created_at", nullable=False, index=True),
    )
    op.create_table(
        "software_releases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("product", sa.String(40), nullable=False),
        sa.Column("version", sa.String(30), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("file_name", sa.String(200), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(200)),
        sa.Column("release_notes", sa.Text()),
        sa.Column("status", sa.String(20), nullable=False),
        _ts("published_at"), _ts("created_at", nullable=False),
        sa.Column("created_by", sa.String(100)),
        sa.UniqueConstraint("product", "version", "platform"),
    )
    op.create_table(
        "download_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.Uuid(), index=True),
        sa.Column("user_id", sa.Uuid()),
        sa.Column("username", sa.String(100)),
        sa.Column("release_id", sa.Uuid()),
        sa.Column("version", sa.String(30)),
        sa.Column("result", sa.String(20), nullable=False),
        sa.Column("ip", sa.String(64)),
        _ts("created_at", nullable=False, index=True),
    )
    op.create_table(
        "user_invites",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        _ts("created_at", nullable=False), _ts("expires_at", nullable=False), _ts("used_at"),
        sa.Column("created_by", sa.String(100)),
    )
    op.create_table(
        "license_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("installation_id", sa.String(64), nullable=False),
        sa.Column("server_url", sa.String(300)),
        sa.Column("installation_secret", sa.String(100)),
        sa.Column("token", sa.Text()),
        _ts("last_check_at"), _ts("last_attempt_at"),
        sa.Column("last_error", sa.String(500)),
        _ts("highest_time_seen"),
        sa.Column("update_available", sa.JSON()),
    )
    op.add_column("companies", sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("customers.id"), index=True))
    op.add_column("users", sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("customers.id"), index=True))
    op.add_column("users", sa.Column("email", sa.String(300)))
    install_db_extras(op.get_bind(), groups=("licensing",))


def downgrade() -> None:
    op.drop_column("users", "email")
    op.drop_column("users", "customer_id")
    op.drop_column("companies", "customer_id")
    for t in ("license_state", "user_invites", "download_events", "software_releases", "license_events",
              "installations", "licenses", "customers"):
        op.drop_table(t)
