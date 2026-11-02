import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSON, IdMixin, TimestampMixin, utcnow


class TallyConnector(TimestampMixin, Base):
    """Per-company Tally connection settings and sync state (PRD §6.2)."""

    __tablename__ = "tally_connectors"

    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("companies.id"), primary_key=True)
    host: Mapped[str] = mapped_column(String(200), default="localhost")
    port: Mapped[int] = mapped_column(Integer, default=9000)
    tally_company_name: Mapped[str] = mapped_column(String(300))
    tally_company_guid: Mapped[str | None] = mapped_column(String(64))
    tdl_version: Mapped[str | None] = mapped_column(String(40))
    # Which stable identifier locates an existing voucher for Alter/Cancel (confirm in Phase 0).
    alter_by: Mapped[str] = mapped_column(String(10), default="masterid")   # masterid | guid | remoteid
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Writes to Tally are a separate switch: lets a company run inbound-only (shadow mode) first.
    outbound_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    inbound_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Emergency freeze switches (PRD §6.5)
    freeze_outbound: Mapped[bool] = mapped_column(Boolean, default=False)
    freeze_inbound: Mapped[bool] = mapped_column(Boolean, default=False)
    frozen_voucher_types: Mapped[list] = mapped_column(JSON, default=list)
    # Change-detection watermarks: Tally AlterID is monotonically increasing per company.
    master_watermark: Mapped[int] = mapped_column(BigInteger, default=0)
    voucher_watermark: Mapped[int] = mapped_column(BigInteger, default=0)
    inbound_from_date: Mapped[date | None] = mapped_column(Date)   # ignore Tally vouchers before this
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voucher_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # last voucher change scan
    voucher_backlog: Mapped[int] = mapped_column(Integer, default=0)   # changed vouchers still to read from Tally
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_status: Mapped[str] = mapped_column(String(20), default="unknown")
    health_message: Mapped[str | None] = mapped_column(Text)
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TallyMapping(IdMixin, TimestampMixin, Base):
    """Stable ERP <-> Tally identity (PRD §6.3)."""

    __tablename__ = "tally_mappings"
    __table_args__ = (
        UniqueConstraint("company_id", "entity_type", "erp_id"),
        UniqueConstraint("company_id", "entity_type", "tally_guid"),
        Index("ix_tally_mappings_master", "company_id", "entity_type", "tally_master_id"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    entity_type: Mapped[str] = mapped_column(String(20))   # voucher | ledger | group | ...
    erp_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    tally_company_key: Mapped[str | None] = mapped_column(String(300))
    tally_guid: Mapped[str | None] = mapped_column(String(100))
    tally_master_id: Mapped[int | None] = mapped_column(BigInteger)
    tally_alter_id: Mapped[int | None] = mapped_column(BigInteger)
    # Human-readable fallback locator – never the sole identity.
    voucher_type: Mapped[str | None] = mapped_column(String(100))
    voucher_number: Mapped[str | None] = mapped_column(String(200))   # voucher number, or the master's current Tally name
    voucher_date: Mapped[date | None] = mapped_column(Date)
    erp_version_synced: Mapped[int | None] = mapped_column(Integer)
    last_synced_hash: Mapped[str | None] = mapped_column(String(64))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_direction: Mapped[str | None] = mapped_column(String(10))   # outbound | inbound


class OutboxStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"           # permanent error – operator queue
    CONFLICT = "conflict"
    SUPERSEDED = "superseded"   # a later event for the same entity already carried this change
    PAUSED = "paused"


class SyncOutbox(IdMixin, Base):
    """Transactional outbox: written in the same transaction as the ERP change (PRD §6.4)."""

    __tablename__ = "sync_outbox"
    __table_args__ = (Index("ix_sync_outbox_claim", "status", "next_attempt_at"),)

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    operation: Mapped[str] = mapped_column(String(10))   # upsert | cancel | delete
    entity_version: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    voucher_type: Mapped[str | None] = mapped_column(String(100))
    voucher_number: Mapped[str | None] = mapped_column(String(200))   # voucher number or master name
    status: Mapped[str] = mapped_column(String(20), default=OutboxStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error_code: Mapped[str | None] = mapped_column(String(40))
    last_error: Mapped[str | None] = mapped_column(Text)
    # An attempt ended without a clear answer (timeout after sending). The next attempt must
    # look the record up in Tally before creating it again.
    outcome_unknown: Mapped[bool] = mapped_column(Boolean, default=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # crash recovery
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncInbox(IdMixin, Base):
    """Every Tally-originated change, stored raw before it is applied (PRD §6.4)."""

    __tablename__ = "sync_inbox"
    __table_args__ = (UniqueConstraint("company_id", "entity_type", "tally_guid", "tally_alter_id"),)

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(20))
    tally_guid: Mapped[str] = mapped_column(String(100))
    tally_master_id: Mapped[int | None] = mapped_column(BigInteger)
    tally_alter_id: Mapped[int] = mapped_column(BigInteger)
    voucher_type: Mapped[str | None] = mapped_column(String(100))
    voucher_number: Mapped[str | None] = mapped_column(String(200))
    payload: Mapped[dict] = mapped_column(JSON)       # normalized canonical form
    raw_xml: Mapped[str | None] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="received")  # received|applied|ignored|failed|conflict
    erp_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    error_code: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncAttempt(IdMixin, Base):
    __tablename__ = "sync_attempts"

    outbox_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sync_outbox.id"), index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    success: Mapped[bool] = mapped_column(Boolean)
    error_code: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(Text)
    request_xml: Mapped[str | None] = mapped_column(Text)
    response_xml: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class SyncConflict(IdMixin, Base):
    __tablename__ = "sync_conflicts"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(20), default="voucher")
    erp_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    tally_guid: Mapped[str | None] = mapped_column(String(100))
    tally_alter_id: Mapped[int | None] = mapped_column(BigInteger)
    detected_by: Mapped[str] = mapped_column(String(10))   # inbound | outbound
    erp_snapshot: Mapped[dict] = mapped_column(JSON)
    tally_snapshot: Mapped[dict] = mapped_column(JSON)
    base_snapshot: Mapped[dict | None] = mapped_column(JSON)   # last version both sides agreed on
    diff: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="open")   # open | resolved
    resolution: Mapped[str | None] = mapped_column(String(10))        # erp | tally | merge
    resolved_by: Mapped[str | None] = mapped_column(String(100))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReconciliationRun(IdMixin, Base):
    __tablename__ = "reconciliation_runs"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    from_date: Mapped[date] = mapped_column(Date)
    to_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="running")   # running | matched | differences | error
    differences: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    started_by: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImportBatch(IdMixin, Base):
    __tablename__ = "import_batches"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))   # masters | vouchers
    status: Mapped[str] = mapped_column(String(30), default="running")   # running | completed[_with_errors] | failed
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    started_by: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    """Append-only; a DB trigger rejects UPDATE/DELETE (PRD §11)."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    actor: Mapped[str] = mapped_column(String(100))
    actor_type: Mapped[str] = mapped_column(String(20))   # user | integration | system
    action: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(500))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Attachment(IdMixin, Base):
    __tablename__ = "attachments"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    file_name: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str | None] = mapped_column(String(100))
    storage_key: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_by: Mapped[str | None] = mapped_column(String(100))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyncProgress(Base):
    """Live progress of the current or last sync run of a company (one row per company).

    Its own table, written on its own connection, so it never waits on locks a running sync holds.
    """

    __tablename__ = "sync_progress"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
