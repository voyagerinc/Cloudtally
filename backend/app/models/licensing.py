"""Cloud licensing (Cloud Licensing PRD §10).

A *customer* is a licensee (the PRD's "company"); it is not the same as `companies`, which are the books
of one Tally company. A customer can have several books companies, hosted here or on its own server.

Server side (tallycloud.voyager.co.in): customers, licences, installations, releases, downloads.
Client side (an installation on the customer's server): the single `license_state` row."""
import uuid
from datetime import date, datetime

from sqlalchemy import JSON, BigInteger, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import IdMixin, TimestampMixin, utcnow


class Customer(IdMixin, TimestampMixin, Base):
    __tablename__ = "customers"

    code: Mapped[str] = mapped_column(String(30), unique=True)          # immutable once created
    legal_name: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="active")   # active | suspended | archived
    contact_name: Mapped[str | None] = mapped_column(String(200))
    contact_email: Mapped[str | None] = mapped_column(String(300))
    contact_phone: Mapped[str | None] = mapped_column(String(50))
    gstin: Mapped[str | None] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text)

    licenses: Mapped[list["License"]] = relationship(back_populates="customer", order_by="License.created_at")


class License(IdMixin, TimestampMixin, Base):
    __tablename__ = "licenses"

    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    product: Mapped[str] = mapped_column(String(40), default="tallycloud")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)      # the key itself is shown once
    key_hint: Mapped[str] = mapped_column(String(10))                   # last characters, to recognise it
    plan: Mapped[str] = mapped_column(String(20))                       # trial | monthly | annual | perpetual
    # pending | active | suspended | revoked | cancelled ("expired" and "grace" follow from the dates)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    starts_at: Mapped[date] = mapped_column(Date)
    expires_at: Mapped[date | None] = mapped_column(Date)               # None: perpetual
    max_users: Mapped[int] = mapped_column(Integer, default=5)          # named users
    max_installations: Mapped[int] = mapped_column(Integer, default=1)
    max_companies: Mapped[int | None] = mapped_column(Integer)          # Tally companies; None: no limit
    modules: Mapped[list] = mapped_column(JSON, default=list)
    offline_grace_days: Mapped[int] = mapped_column(Integer, default=7)
    heartbeat_hours: Mapped[int] = mapped_column(Integer, default=24)
    min_version: Mapped[str | None] = mapped_column(String(30))
    max_version: Mapped[str | None] = mapped_column(String(30))
    created_by: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)

    customer: Mapped[Customer] = relationship(back_populates="licenses")
    installations: Mapped[list["Installation"]] = relationship(back_populates="license", order_by="Installation.activated_at")


class Installation(IdMixin, Base):
    __tablename__ = "installations"
    __table_args__ = (UniqueConstraint("license_id", "installation_id"),)

    license_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("licenses.id"), index=True)
    installation_id: Mapped[str] = mapped_column(String(64))            # made by the client at first start
    secret_hash: Mapped[str] = mapped_column(String(64))                # proves later calls come from it
    server_label: Mapped[str | None] = mapped_column(String(200))
    hostname: Mapped[str | None] = mapped_column(String(200))
    product_version: Mapped[str | None] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="active")   # active | deactivated | blocked
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ip: Mapped[str | None] = mapped_column(String(64))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    license: Mapped[License] = relationship(back_populates="installations")


class LicenseEvent(Base):
    """Activation, heartbeat and admin events per licence. Append-only (a DB trigger rejects changes)."""
    __tablename__ = "license_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    license_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    installation_id: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(40))
    result: Mapped[str] = mapped_column(String(20), default="ok")       # ok | rejected
    actor: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SoftwareRelease(IdMixin, Base):
    __tablename__ = "software_releases"
    __table_args__ = (UniqueConstraint("product", "version", "platform"),)

    product: Mapped[str] = mapped_column(String(40), default="tallycloud")
    version: Mapped[str] = mapped_column(String(30))
    platform: Mapped[str] = mapped_column(String(20), default="windows")
    file_name: Mapped[str] = mapped_column(String(200))
    file_size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str | None] = mapped_column(String(200))          # Ed25519 over the SHA-256
    release_notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")    # draft | published | withdrawn
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(100))


class DownloadEvent(Base):
    __tablename__ = "download_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    username: Mapped[str | None] = mapped_column(String(100))
    release_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    version: Mapped[str | None] = mapped_column(String(30))
    result: Mapped[str] = mapped_column(String(20))                     # ok | denied
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class UserInvite(IdMixin, Base):
    """One-time link with which a new company admin sets their own password (no passwords by e-mail)."""
    __tablename__ = "user_invites"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(100))


class LicenseState(Base):
    """Client side: this installation's identity and the last signed entitlement from the cloud."""
    __tablename__ = "license_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    installation_id: Mapped[str] = mapped_column(String(64))
    server_url: Mapped[str | None] = mapped_column(String(300))
    installation_secret: Mapped[str | None] = mapped_column(String(100))
    token: Mapped[str | None] = mapped_column(Text)                     # signed entitlement
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # last successful check
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    highest_time_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # clock moved back?
    update_available: Mapped[dict | None] = mapped_column(JSON)
