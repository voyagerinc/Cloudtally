"""Applying the licence inside the software: modules, read-only mode, named-user and company limits.

Where the licence comes from depends on ERP_LICENSE_MODE:
  off     no checks (development, tests, or a server that is not licensed)
  client  this installation's signed entitlement from the cloud (license_state row)
  server  books hosted on the cloud server follow the licence of the customer they belong to;
          books without a customer (the owner's own) are not limited."""
import threading
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.licensing.core import FULL, MODULES, WRITE_PERMS, Entitlement, entitlement_from_license, \
    entitlement_from_token, effective_status, module_of, utc, verify
from app.models import Company, Customer, License, LicenseState, User, UserCompanyAccess
from app.models.base import utcnow

_cache: dict = {"at": 0.0, "ent": None}
_lock = threading.Lock()
CACHE_SECONDS = 30


def forget_cached() -> None:
    """Call after the installation's licence changed (activation, check, deactivation)."""
    with _lock:
        _cache["at"], _cache["ent"] = 0.0, None


def installation_entitlement(db: Session) -> Entitlement:
    with _lock:
        if _cache["ent"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["ent"]
    state = db.get(LicenseState, 1)
    payload = verify(state.token, settings.license_public_key) if state and state.token else None
    if payload is None:
        ent = Entitlement(state="not_activated", read_only=True,
                          reason="This installation is not activated yet. The super admin enters the licence key "
                                 "on the Licence tab. Until then the software is read-only.")
    else:
        # A clock set back must not stretch the grace period: use the latest time this server has seen.
        now = max(utcnow(), utc(state.highest_time_seen) or utcnow())
        ent = entitlement_from_token(payload, state.installation_id, utc(state.last_check_at), now)
    with _lock:
        _cache["at"], _cache["ent"] = time.monotonic(), ent
    return ent


def best_license(db: Session, customer_id: uuid.UUID) -> License | None:
    """The customer's licence that counts now: a usable one first, then the most recent."""
    today = utcnow().date()
    rank = {"active": 0, "pending": 1, "expired": 2, "suspended": 3, "cancelled": 4, "revoked": 5}
    lics = list(db.scalars(select(License).where(License.customer_id == customer_id)))
    if not lics:
        return None
    return min(lics, key=lambda lic: (rank.get(effective_status(lic.status, lic.starts_at, lic.expires_at, today), 9),
                                      -(lic.expires_at.toordinal() if lic.expires_at else 10 ** 7)))


def customer_entitlement(db: Session, customer_id: uuid.UUID) -> Entitlement:
    customer = db.get(Customer, customer_id)
    if customer is None or customer.status != "active":
        return Entitlement(state="read_only", read_only=True,
                           reason="This customer account is suspended. The software is read-only; contact Voyager.")
    lic = best_license(db, customer_id)
    if lic is None:
        return Entitlement(state="read_only", read_only=True, customer=customer.legal_name,
                           reason="There is no licence for this customer yet. The software is read-only.")
    return entitlement_from_license(lic, utcnow().date())


def entitlement_for(db: Session, company_id: uuid.UUID | None = None) -> Entitlement:
    if settings.license_mode == "client":
        return installation_entitlement(db)
    if settings.license_mode == "server" and company_id is not None:
        company = db.get(Company, company_id)
        if company is not None and company.customer_id is not None:
            return customer_entitlement(db, company.customer_id)
    return FULL


def check_perm(db: Session, company_id: uuid.UUID | None, perm: str) -> None:
    ent = entitlement_for(db, company_id)
    module = module_of(perm)
    if not ent.allows_module(module):
        raise HTTPException(403, f"Your licence does not include {MODULES[module]}. Contact Voyager to add it.")
    if ent.read_only and perm in WRITE_PERMS:
        raise HTTPException(403, ent.reason)


def _customer_of(db: Session, company_id: uuid.UUID | None) -> uuid.UUID | None:
    if settings.license_mode != "server" or company_id is None:
        return None
    company = db.get(Company, company_id)
    return company.customer_id if company else None


def named_users(db: Session, company_id: uuid.UUID | None) -> int:
    """People counted against the licence: switched-on users who sign in with a password."""
    stmt = select(func.count(func.distinct(User.id))).where(User.is_active.is_(True), User.password_hash.is_not(None))
    customer_id = _customer_of(db, company_id)
    if customer_id is not None:
        stmt = stmt.join(UserCompanyAccess, UserCompanyAccess.user_id == User.id).join(
            Company, Company.id == UserCompanyAccess.company_id).where(Company.customer_id == customer_id)
    return db.scalar(stmt) or 0


def check_user_limit(db: Session, company_id: uuid.UUID | None, user: User | None = None) -> None:
    """Before switching on or adding a user (`user`: an existing one, who may already be counted)."""
    ent = entitlement_for(db, company_id)
    if ent.max_users is None:
        return
    used = named_users(db, company_id)
    already = False
    if user is not None and user.is_active and user.password_hash:
        customer_id = _customer_of(db, company_id)
        already = customer_id is None or any(
            (c := db.get(Company, a.company_id)) is not None and c.customer_id == customer_id for a in user.access)
    if not already and used >= ent.max_users:
        raise HTTPException(403, f"Your licence allows {ent.max_users} users and {used} are in use. "
                                 "Switch a user off, or contact Voyager for more users.")


def check_company_limit(db: Session, customer_id: uuid.UUID | None = None) -> None:
    """Before linking another Tally company."""
    if settings.license_mode == "client":
        ent, count = installation_entitlement(db), db.scalar(select(func.count(Company.id)))
    elif settings.license_mode == "server" and customer_id is not None:
        ent = customer_entitlement(db, customer_id)
        count = db.scalar(select(func.count(Company.id)).where(Company.customer_id == customer_id))
    else:
        return
    if ent.read_only:
        raise HTTPException(403, ent.reason)
    if ent.max_companies is not None and count >= ent.max_companies:
        raise HTTPException(403, f"Your licence allows {ent.max_companies} Tally companies. Contact Voyager for more.")


def sync_allowed(db: Session, company_id: uuid.UUID) -> bool:
    ent = entitlement_for(db, company_id)
    return ent.allows_module("tally_sync") and not ent.read_only
