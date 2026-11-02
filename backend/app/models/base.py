import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# PRD §7.1: NUMERIC for money and quantity, never floating point.
Money = Numeric(18, 2)
Qty = Numeric(18, 4)
Rate = Numeric(18, 6)
JSON = JSONB


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


ZERO = Decimal("0")


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
