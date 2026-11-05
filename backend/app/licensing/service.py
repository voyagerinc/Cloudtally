"""Server side of licensing: activation, heartbeat, signed entitlements, invitations, notices."""
import logging
import secrets
import smtplib
import threading
import time
from collections import defaultdict, deque
from datetime import date, timedelta
from email.message import EmailMessage

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_key
from app.licensing.core import effective_status, sign, version_allowed, version_tuple
from app.models import Installation, License, LicenseEvent, SoftwareRelease, User, UserInvite
from app.models.base import utcnow

log = logging.getLogger(__name__)

INVITE_HOURS = 72


# ------------------------------------------------------------------ rate limiting (per client address)

class RateLimit:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                raise HTTPException(429, "Too many requests. Wait a minute and try again.")
            q.append(now)


activation_limit = RateLimit(20)
setup_limit = RateLimit(10)


# ------------------------------------------------------------------ events and notices

def event(db: Session, event_type: str, *, lic: License | None = None, customer_id=None, installation_id=None,
          result: str = "ok", actor: str | None = None, ip: str | None = None, **details) -> None:
    db.add(LicenseEvent(license_id=lic.id if lic else None,
                        customer_id=customer_id or (lic.customer_id if lic else None),
                        installation_id=installation_id, event_type=event_type, result=result, actor=actor,
                        details=details or None, ip=ip))


def send_mail(to: str | None, subject: str, body: str) -> bool:
    """E-mail when SMTP is configured; otherwise the portal shows the notice and links to copy."""
    if not (settings.smtp_host and settings.smtp_from and to):
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
            s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password or "")
            s.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        log.exception("could not send e-mail to %s", to)
        return False


def new_invite(db: Session, user: User, by: str) -> str:
    """One-time link (72 h) with which the user sets their own password. Older links stop working."""
    for old in db.scalars(select(UserInvite).where(UserInvite.user_id == user.id, UserInvite.used_at.is_(None))):
        old.used_at = utcnow()
    token = secrets.token_urlsafe(32)
    db.add(UserInvite(user_id=user.id, token_hash=hash_key(token), expires_at=utcnow() + timedelta(hours=INVITE_HOURS),
                      created_by=by))
    return f"{settings.public_url.rstrip('/')}/#setup={token}"


def invite_for(db: Session, token: str) -> UserInvite:
    inv = db.scalar(select(UserInvite).where(UserInvite.token_hash == hash_key(token)))
    if inv is None or inv.used_at is not None or inv.expires_at <= utcnow():
        raise HTTPException(404, "This link has expired or was already used. Ask Voyager for a new one.")
    return inv


# ------------------------------------------------------------------ licences

def default_expiry(plan: str, start: date) -> date | None:
    if plan == "perpetual":
        return None
    if plan == "trial":
        return start + timedelta(days=30)
    months = 1 if plan == "monthly" else 12
    y, m = divmod(start.month - 1 + months, 12)
    year, month = start.year + y, m + 1
    for day in (start.day, 30, 29, 28):
        try:
            return date(year, month, day) - timedelta(days=1)
        except ValueError:
            continue


def active_installations(db: Session, lic: License) -> int:
    return db.scalar(select(func.count(Installation.id)).where(Installation.license_id == lic.id,
                                                               Installation.status == "active")) or 0


def latest_release_for(db: Session, lic: License, current: str | None) -> SoftwareRelease | None:
    """The newest published release this licence may use, if newer than `current`."""
    best = None
    for r in db.scalars(select(SoftwareRelease).where(SoftwareRelease.status == "published",
                                                      SoftwareRelease.product == lic.product)):
        if version_allowed(r.version, lic.min_version, lic.max_version) and \
                (best is None or version_tuple(r.version) > version_tuple(best.version)):
            best = r
    if best and current and version_tuple(best.version) <= version_tuple(current):
        return None
    return best


def entitlement_token(db: Session, lic: License, inst: Installation) -> dict:
    today = utcnow().date()
    payload = {
        "license_id": str(lic.id), "customer_code": lic.customer.code, "customer_name": lic.customer.legal_name,
        "installation_id": inst.installation_id, "product": lic.product, "plan": lic.plan,
        "status": "suspended" if lic.customer.status != "active" else effective_status(
            lic.status, lic.starts_at, lic.expires_at, today),
        "starts_at": lic.starts_at.isoformat(), "expires_at": lic.expires_at.isoformat() if lic.expires_at else None,
        "modules": sorted(lic.modules or []), "max_users": lic.max_users, "max_companies": lic.max_companies,
        "offline_grace_days": lic.offline_grace_days, "heartbeat_hours": lic.heartbeat_hours,
        "min_version": lic.min_version, "max_version": lic.max_version, "issued_at": utcnow().isoformat(),
    }
    newer = latest_release_for(db, lic, inst.product_version)
    return {
        "token": sign(payload), "server_time": utcnow().isoformat(), "heartbeat_hours": lic.heartbeat_hours,
        "update": {"version": newer.version, "release_notes": newer.release_notes} if newer else None,
    }


def refuse(db: Session, kind: str, message: str, status: int = 403, **kw):
    event(db, kind, result="rejected", reason=message, **kw)
    db.commit()
    raise HTTPException(status, message)


# ------------------------------------------------------------------ expiry notices (licence portal)

def send_expiry_notices(db: Session) -> int:
    """E-mail customers 30/15/7/1 days before expiry, once per licence and step. Returns notices sent."""
    from app.licensing.core import EXPIRY_WARN_DAYS
    today = utcnow().date()
    sent = 0
    for lic in db.scalars(select(License).where(License.expires_at.is_not(None))):
        if effective_status(lic.status, lic.starts_at, lic.expires_at, today) != "active":
            continue
        left = (lic.expires_at - today).days
        step = next((d for d in sorted(EXPIRY_WARN_DAYS) if left <= d), None)
        if step is None:
            continue
        done = db.scalar(select(func.count(LicenseEvent.id)).where(
            LicenseEvent.license_id == lic.id, LicenseEvent.event_type == "expiry_notice",
            LicenseEvent.details["step"].as_integer() == step,
            LicenseEvent.details["expires_at"].as_string() == lic.expires_at.isoformat()))
        if done:
            continue
        mailed = send_mail(lic.customer.contact_email, f"Tally Cloud licence expires in {left} day(s)",
                           f"The Tally Cloud licence of {lic.customer.legal_name} expires on "
                           f"{lic.expires_at:%d %b %Y}. Renew it to keep working without interruption: sign in "
                           f"to {settings.public_url} and click Request renewal.")
        event(db, "expiry_notice", lic=lic, step=step, expires_at=lic.expires_at.isoformat(), emailed=mailed)
        sent += 1
    db.commit()
    return sent


class ExpiryNotices:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="expiry-notices", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        from app.core.db import SessionLocal
        while not self._stop.wait(60):
            try:
                with SessionLocal() as db:
                    send_expiry_notices(db)
            except Exception:
                log.exception("expiry notices failed")
            self._stop.wait(6 * 3600)


expiry_notices = ExpiryNotices()
