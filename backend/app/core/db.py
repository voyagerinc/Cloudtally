from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Database-level guards that the ORM cannot express. Shared by the Alembic
# migration and by test setup so both build the same schema.
DB_EXTRAS_SQL = [
    # PRD §11: audit log must not be modifiable through application roles.
    """
    CREATE OR REPLACE FUNCTION audit_events_immutable() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'audit_events is append-only';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_audit_events_immutable ON audit_events",
    """
    CREATE TRIGGER trg_audit_events_immutable
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_immutable()
    """,
    # Same rule for voucher history: edits create versions, never rewrite them.
    "DROP TRIGGER IF EXISTS trg_voucher_versions_immutable ON voucher_versions",
    """
    CREATE TRIGGER trg_voucher_versions_immutable
    BEFORE UPDATE ON voucher_versions
    FOR EACH ROW EXECUTE FUNCTION audit_events_immutable()
    """,
]


# Licence events are history too (Cloud Licensing PRD §12): nobody rewrites or deletes them.
LICENSING_EXTRAS_SQL = [
    "DROP TRIGGER IF EXISTS trg_license_events_immutable ON license_events",
    """
    CREATE TRIGGER trg_license_events_immutable
    BEFORE UPDATE OR DELETE ON license_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_immutable()
    """,
]


def install_db_extras(conn: Connection, groups: tuple[str, ...] = ("core", "licensing")) -> None:
    """groups: "core" (initial schema) and/or "licensing" (migration 0009 onwards)."""
    for group, statements in (("core", DB_EXTRAS_SQL), ("licensing", LICENSING_EXTRAS_SQL)):
        if group in groups:
            for stmt in statements:
                conn.execute(text(stmt))
