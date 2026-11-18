"""Management (MIS) reports: how ledgers, cost centres and stock groups map to divisions, and deferred expenses."""
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSON, IdMixin, Money, TimestampMixin


class MisSettings(TimestampMixin, Base):
    """One row per company. config holds divisions, cost-centre / stock-group / ledger mappings and stock
    value overrides (see app.reports.mis for the shape)."""
    __tablename__ = "mis_settings"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(200))


class DeferredExpense(IdMixin, TimestampMixin, Base):
    """A prepaid cost spread evenly over months: the P&L takes total/months each month from first_month."""
    __tablename__ = "deferred_expenses"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    ledger_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledgers.id", ondelete="SET NULL"))
    head: Mapped[str] = mapped_column(String(200))          # expense head shown in the reports
    description: Mapped[str | None] = mapped_column(Text)
    total_amount: Mapped[Decimal] = mapped_column(Money)
    first_month: Mapped[date] = mapped_column(Date)          # always the 1st of a month
    months: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str | None] = mapped_column(String(200))
