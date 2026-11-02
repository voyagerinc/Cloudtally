import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSON, ZERO, IdMixin, Money, Qty, Rate, TimestampMixin, utcnow
from app.models.masters import Ledger, StockItem, VoucherType


class VoucherStatus:
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    POSTED = "posted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    DELETED = "deleted"

    # Statuses whose figures count in reports (cancelled vouchers keep their row but carry no value).
    REPORTABLE = (POSTED,)


class SyncStatus:
    NOT_SYNCED = "not_synced"   # never needs sync (draft etc.) or not yet queued
    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"
    CONFLICT = "conflict"


class Voucher(IdMixin, TimestampMixin, Base):
    __tablename__ = "vouchers"
    __table_args__ = (
        Index("ix_vouchers_company_date", "company_id", "voucher_date"),
        Index("ix_vouchers_number", "company_id", "voucher_type_id", "voucher_number"),
        Index("ix_vouchers_sync", "company_id", "sync_status"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    financial_year_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("financial_years.id"), index=True)
    voucher_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("voucher_types.id"))
    voucher_number: Mapped[str] = mapped_column(String(60))
    voucher_date: Mapped[date] = mapped_column(Date)
    effective_date: Mapped[date | None] = mapped_column(Date)
    reference_no: Mapped[str | None] = mapped_column(String(100))
    reference_date: Mapped[date | None] = mapped_column(Date)
    party_ledger_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledgers.id"))
    narration: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    exchange_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("1"))
    place_of_supply: Mapped[str | None] = mapped_column(String(100))
    is_invoice: Mapped[bool] = mapped_column(default=False)
    is_reverse_charge: Mapped[bool] = mapped_column(default=False)
    statutory: Mapped[dict] = mapped_column(JSON, default=dict)   # e-invoice IRN, e-way bill no., etc.

    status: Mapped[str] = mapped_column(String(20), default=VoucherStatus.DRAFT, index=True)
    sync_status: Mapped[str] = mapped_column(String(20), default=SyncStatus.NOT_SYNCED)
    source_system: Mapped[str] = mapped_column(String(10), default="erp")   # where it was originated
    version: Mapped[int] = mapped_column(Integer, default=1)                # optimistic concurrency
    content_hash: Mapped[str | None] = mapped_column(String(64))

    created_by: Mapped[str | None] = mapped_column(String(100))
    updated_by: Mapped[str | None] = mapped_column(String(100))
    approved_by: Mapped[str | None] = mapped_column(String(100))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    voucher_type: Mapped[VoucherType] = relationship()
    party_ledger: Mapped[Ledger | None] = relationship()
    entries: Mapped[list["VoucherEntry"]] = relationship(
        back_populates="voucher", cascade="all, delete-orphan", order_by="VoucherEntry.line_no"
    )
    inventory_lines: Mapped[list["VoucherInventoryLine"]] = relationship(
        back_populates="voucher", cascade="all, delete-orphan", order_by="VoucherInventoryLine.line_no"
    )


class VoucherEntry(IdMixin, Base):
    """Accounting line. amount is signed: debit positive, credit negative."""

    __tablename__ = "voucher_entries"
    __table_args__ = (CheckConstraint("amount <> 0", name="ck_entry_nonzero"),)

    voucher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vouchers.id", ondelete="CASCADE"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    ledger_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledgers.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    narration: Mapped[str | None] = mapped_column(Text)
    gst: Mapped[dict] = mapped_column(JSON, default=dict)   # duty head, rate, taxable value

    voucher: Mapped[Voucher] = relationship(back_populates="entries")
    ledger: Mapped[Ledger] = relationship()
    bill_allocations: Mapped[list["BillAllocation"]] = relationship(cascade="all, delete-orphan")
    cost_allocations: Mapped[list["CostAllocation"]] = relationship(cascade="all, delete-orphan")
    bank_allocations: Mapped[list["BankAllocation"]] = relationship(cascade="all, delete-orphan")


class BillAllocation(IdMixin, Base):
    __tablename__ = "bill_allocations"

    entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("voucher_entries.id", ondelete="CASCADE"), index=True)
    ref_type: Mapped[str] = mapped_column(String(20))   # New Ref | Agst Ref | Advance | On Account
    name: Mapped[str | None] = mapped_column(String(100), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    due_date: Mapped[date | None] = mapped_column(Date)


class CostAllocation(IdMixin, Base):
    __tablename__ = "cost_allocations"

    entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("voucher_entries.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(200), default="Primary Cost Category")
    cost_centre: Mapped[str] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Money)


class BankAllocation(IdMixin, Base):
    __tablename__ = "bank_allocations"

    entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("voucher_entries.id", ondelete="CASCADE"), index=True)
    transaction_type: Mapped[str | None] = mapped_column(String(40))   # Cheque, NEFT, RTGS, UPI ...
    instrument_no: Mapped[str | None] = mapped_column(String(60))
    instrument_date: Mapped[date | None] = mapped_column(Date)
    bank_name: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Money)
    clearing_date: Mapped[date | None] = mapped_column(Date)            # bank reconciliation


class VoucherInventoryLine(IdMixin, Base):
    __tablename__ = "voucher_inventory_lines"

    voucher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vouchers.id", ondelete="CASCADE"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    stock_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stock_items.id"), index=True)
    godown: Mapped[str | None] = mapped_column(String(200))
    batch: Mapped[str | None] = mapped_column(String(100))
    # Signed: inward positive, outward negative.
    quantity: Mapped[Decimal] = mapped_column(Qty)
    unit: Mapped[str | None] = mapped_column(String(40))
    rate: Mapped[Decimal] = mapped_column(Rate, default=ZERO)
    discount_pct: Mapped[Decimal] = mapped_column(Rate, default=ZERO)
    amount: Mapped[Decimal] = mapped_column(Money, default=ZERO)
    ledger_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledgers.id"))   # accounting allocation
    # Direction of a value-only line (quantity 0); otherwise the sign of `quantity` says it.
    inward: Mapped[bool | None] = mapped_column(Boolean)

    voucher: Mapped[Voucher] = relationship(back_populates="inventory_lines")
    stock_item: Mapped[StockItem] = relationship()
    ledger: Mapped[Ledger | None] = relationship()


class VoucherVersion(IdMixin, Base):
    """Immutable snapshot of every voucher version (PRD §5.3)."""

    __tablename__ = "voucher_versions"
    __table_args__ = (Index("ix_voucher_versions_vid", "voucher_id", "version", unique=True),)

    voucher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vouchers.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    snapshot: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    changed_by: Mapped[str | None] = mapped_column(String(100))
    source_system: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
