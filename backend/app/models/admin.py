import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import IdMixin, TimestampMixin


class Company(IdMixin, TimestampMixin, Base):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(200), unique=True)
    legal_name: Mapped[str | None] = mapped_column(String(300))
    address: Mapped[str | None] = mapped_column(String(1000))
    gstin: Mapped[str | None] = mapped_column(String(20))
    pan: Mapped[str | None] = mapped_column(String(20))
    state: Mapped[str | None] = mapped_column(String(100))
    base_currency: Mapped[str] = mapped_column(String(3), default="INR")
    books_from: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")

    financial_years: Mapped[list["FinancialYear"]] = relationship(back_populates="company", order_by="FinancialYear.start_date")


class FinancialYear(IdMixin, Base):
    __tablename__ = "financial_years"
    __table_args__ = (UniqueConstraint("company_id", "start_date"),)

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Period lock: no voucher on or before this date may be created/edited.
    locked_upto: Mapped[date | None] = mapped_column(Date)

    company: Mapped[Company] = relationship(back_populates="financial_years")


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(100), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    access: Mapped[list["UserCompanyAccess"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserCompanyAccess(Base):
    __tablename__ = "user_company_access"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("companies.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(40))
    # Optional maker-checker: vouchers above this amount need approval for this user.
    approval_limit: Mapped[int | None] = mapped_column(Integer)

    user: Mapped[User] = relationship(back_populates="access")
