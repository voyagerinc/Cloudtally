"""Cloud licensing API (Cloud Licensing PRD §9).

  /licenses/*            called by installations (licence key once, then installation id + secret)
  /portal/*, /downloads  the customer's company admin: own licence and installers only
  /admin/*               the super admin: customers, licences, installations, releases, reports
Every customer-owned record is reached through the signed-in user's customer, never an id the client
sends, so one customer can never see another's (PRD FR-19)."""
import hashlib
import hmac
import os
import re
import secrets
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app import audit
from app.api.deps import DB, CurrentUser, actor_of
from app.core.config import settings
from app.core.security import check_password_rules, hash_key, hash_password
from app.licensing import service
from app.licensing.core import (
    MODULES, effective_status, hash_license_key, new_license_key, sign_bytes, version_allowed,
)
from app.licensing.enforce import customer_entitlement, live_users
from app.models import (
    Company, Customer, DownloadEvent, Installation, License, LicenseEvent, SoftwareRelease, User,
    UserCompanyAccess, UserSession,
)
from app.models.base import utcnow

router = APIRouter(tags=["licensing"])


def _ip(request: Request) -> str | None:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else None)


def _server_only() -> None:
    if settings.license_mode != "server":
        raise HTTPException(404, "This server is not the licence portal")


def _superadmin(user: User) -> None:
    _server_only()
    if not user.is_superadmin:
        raise HTTPException(403, "Only the Voyager super admin can do this")


# ================================================================== installations (client → cloud)

class ActivateIn(BaseModel):
    license_key: str = Field(max_length=60)
    installation_id: str = Field(min_length=16, max_length=64)
    server_label: str | None = Field(None, max_length=200)
    hostname: str | None = Field(None, max_length=200)
    product_version: str | None = Field(None, max_length=30)


class InstallationIn(BaseModel):
    installation_id: str = Field(max_length=64)
    installation_secret: str = Field(max_length=100)
    product_version: str | None = Field(None, max_length=30)


@router.post("/licenses/activate")
def activate(body: ActivateIn, request: Request, db: DB):
    _server_only()
    ip = _ip(request)
    service.activation_limit.check(f"act:{ip}")
    lic = db.scalar(select(License).where(License.key_hash == hash_license_key(body.license_key)))
    ctx = {"installation_id": body.installation_id, "ip": ip}
    if lic is None:
        service.refuse(db, "activate", "This licence key is not recognised. Check it and try again.", 404, **ctx)
    ctx["lic"] = lic
    today = utcnow().date()
    status = effective_status(lic.status, lic.starts_at, lic.expires_at, today)
    if lic.customer.status != "active":
        service.refuse(db, "activate", "This customer account is suspended. Contact Voyager.", **ctx)
    if status == "pending" and lic.starts_at > today:
        service.refuse(db, "activate", f"This licence starts on {lic.starts_at:%d %b %Y}.", **ctx)
    if status in ("expired", "suspended", "revoked", "cancelled"):
        service.refuse(db, "activate", f"This licence is {status}. Contact Voyager to renew or replace it.", **ctx)
    if not version_allowed(body.product_version, lic.min_version, lic.max_version):
        service.refuse(db, "activate", f"Version {body.product_version} is not covered by this licence. "
                                       "Install a version from the licence portal.", **ctx)
    inst = db.scalar(select(Installation).where(Installation.license_id == lic.id,
                                                Installation.installation_id == body.installation_id))
    if inst is not None and inst.status == "blocked":
        service.refuse(db, "activate", "This server has been blocked for this licence. Contact Voyager.", **ctx)
    if (inst is None or inst.status != "active") and service.active_installations(db, lic) >= lic.max_installations:
        service.refuse(db, "activate", f"This licence is already active on {lic.max_installations} server(s), the "
                                       "maximum. Deactivate the old server first (Licence tab), or ask Voyager to "
                                       "release it.", **ctx)
    secret = secrets.token_urlsafe(32)
    if inst is None:
        inst = Installation(license_id=lic.id, installation_id=body.installation_id, secret_hash="")
        db.add(inst)
    inst.secret_hash, inst.status, inst.deactivated_at = hash_key(secret), "active", None
    inst.server_label, inst.hostname, inst.product_version = body.server_label, body.hostname, body.product_version
    inst.activated_at = inst.last_seen_at = utcnow()
    inst.last_ip = ip
    if lic.status == "pending":
        lic.status = "active"
    db.flush()
    service.event(db, "activate", server_label=body.server_label, version=body.product_version, **ctx)
    out = service.entitlement_token(db, lic, inst)
    db.commit()
    service.send_mail(lic.customer.contact_email, "Tally Cloud activated",
                      f"Tally Cloud was activated on {body.server_label or body.hostname or 'a server'} "
                      f"for {lic.customer.legal_name}.")
    return {**out, "installation_secret": secret}


def _installation(db, body: InstallationIn, request: Request, kind: str) -> tuple[License, Installation]:
    _server_only()
    ip = _ip(request)
    service.activation_limit.check(f"inst:{ip}")
    inst = db.scalar(select(Installation).where(Installation.installation_id == body.installation_id,
                                                Installation.secret_hash == hash_key(body.installation_secret)))
    if inst is None:
        service.refuse(db, kind, "This server is not activated. Activate it with the licence key.", 401,
                       installation_id=body.installation_id, ip=ip)
    if inst.status != "active":
        service.refuse(db, kind, "This server's activation was " + ("blocked" if inst.status == "blocked" else "released")
                       + ". Activate it again with the licence key, or contact Voyager.", 403,
                       lic=inst.license, installation_id=inst.installation_id, ip=ip)
    inst.last_seen_at, inst.last_ip = utcnow(), ip
    if body.product_version:
        inst.product_version = body.product_version
    return inst.license, inst


@router.post("/licenses/heartbeat")
def heartbeat(body: InstallationIn, request: Request, db: DB):
    lic, inst = _installation(db, body, request, "heartbeat")
    service.event(db, "heartbeat", lic=lic, installation_id=inst.installation_id, ip=inst.last_ip,
                  version=inst.product_version)
    out = service.entitlement_token(db, lic, inst)
    db.commit()
    return out


@router.post("/licenses/validate")
def validate(body: InstallationIn, request: Request, db: DB):
    lic, inst = _installation(db, body, request, "validate")
    out = service.entitlement_token(db, lic, inst)
    db.commit()
    return out


@router.post("/licenses/deactivate")
def deactivate(body: InstallationIn, request: Request, db: DB):
    lic, inst = _installation(db, body, request, "deactivate")
    inst.status, inst.deactivated_at = "deactivated", utcnow()
    service.event(db, "deactivate", lic=lic, installation_id=inst.installation_id, ip=inst.last_ip, by="installation")
    db.commit()
    return {"ok": True}


# ================================================================== invitations (company admin sets own password)

class SetupIn(BaseModel):
    token: str = Field(max_length=100)
    password: str = Field(max_length=200)
    username: str | None = Field(None, max_length=50)    # the admin may pick their own user ID here


@router.get("/auth/setup/{token}")
def setup_info(token: str, request: Request, db: DB):
    service.setup_limit.check(f"setup:{_ip(request)}")
    inv = service.invite_for(db, token)
    u = db.get(User, inv.user_id)
    return {"username": u.username, "display_name": u.display_name}


@router.post("/auth/setup")
def setup_password(body: SetupIn, request: Request, db: DB):
    service.setup_limit.check(f"setup:{_ip(request)}")
    inv = service.invite_for(db, body.token)
    u = db.get(User, inv.user_id)
    from app.api.users import USERNAME, _find_user
    wanted = (body.username or "").strip()
    if wanted and wanted.lower() != u.username.lower():
        if not USERNAME.match(wanted):
            raise HTTPException(422, "User ID: 3 to 50 letters, digits or . _ - @, starting with a letter or digit")
        if _find_user(db, wanted):
            raise HTTPException(409, "This user ID is already taken; choose another")
        audit.record(db, actor_of(u), "user.rename", "user", u.id, before={"username": u.username},
                     after={"username": wanted})
        u.username = wanted
    elif wanted:
        u.username = wanted                     # same ID, maybe other capitals
    if reason := check_password_rules(body.password, u.username):
        raise HTTPException(422, reason)
    u.password_hash, u.must_change_password, u.is_active = hash_password(body.password), False, True
    u.failed_logins, u.locked_until = 0, None
    inv.used_at = utcnow()
    db.query(UserSession).filter(UserSession.user_id == u.id).delete()
    audit.record(db, actor_of(u), "user.password_setup", "user", u.id)
    db.commit()
    return {"username": u.username}


# ================================================================== customer portal (company admin)

def _license_out(db, lic: License, *, admin: bool = False) -> dict:
    today = utcnow().date()
    out = {
        "id": lic.id, "plan": lic.plan, "status": lic.status,
        "effective_status": effective_status(lic.status, lic.starts_at, lic.expires_at, today),
        "starts_at": lic.starts_at, "expires_at": lic.expires_at,
        "days_left": (lic.expires_at - today).days if lic.expires_at else None,
        "key_hint": f"…{lic.key_hint}", "modules": [{"code": m, "label": MODULES.get(m, m)} for m in lic.modules or []],
        "max_users": lic.max_users, "max_installations": lic.max_installations, "max_companies": lic.max_companies,
        "offline_grace_days": lic.offline_grace_days, "heartbeat_hours": lic.heartbeat_hours,
        "min_version": lic.min_version, "max_version": lic.max_version,
        "installations": [{
            "id": i.id, "installation_id": i.installation_id, "server_label": i.server_label, "hostname": i.hostname,
            "product_version": i.product_version, "status": i.status, "activated_at": i.activated_at,
            "last_seen_at": i.last_seen_at, **({"last_ip": i.last_ip} if admin else {})} for i in lic.installations],
        "active_installations": sum(1 for i in lic.installations if i.status == "active"),
    }
    if admin:
        out.update(notes=lic.notes, created_by=lic.created_by, created_at=lic.created_at,
                   customer_id=lic.customer_id, customer=lic.customer.legal_name)
    return out


def _my_customer(db, user: User, customer_id: uuid.UUID | None = None) -> Customer:
    """The signed-in company admin's own customer (the super admin may look at any)."""
    _server_only()
    cid = customer_id if user.is_superadmin and customer_id else user.customer_id
    customer = db.get(Customer, cid) if cid else None
    if customer is None:
        raise HTTPException(404, "No licence account is linked to your user")
    return customer


def _release_allowed(db, customer: Customer, rel: SoftwareRelease) -> bool:
    if rel.status != "published" or customer.status != "active":
        return False
    today = utcnow().date()
    return any(effective_status(lic.status, lic.starts_at, lic.expires_at, today) in ("active", "pending")
               and lic.product == rel.product and version_allowed(rel.version, lic.min_version, lic.max_version)
               for lic in customer.licenses)


@router.get("/portal/license")
def portal_license(db: DB, user: CurrentUser, customer_id: uuid.UUID | None = None):
    customer = _my_customer(db, user, customer_id)
    hosted = list(db.scalars(select(Company).where(Company.customer_id == customer.id)))
    ent = customer_entitlement(db, customer.id)
    return {
        "customer": {"id": customer.id, "code": customer.code, "legal_name": customer.legal_name,
                     "status": customer.status, "contact_name": customer.contact_name,
                     "contact_email": customer.contact_email, "contact_phone": customer.contact_phone,
                     "gstin": customer.gstin},
        "licenses": [_license_out(db, lic) for lic in customer.licenses if lic.status != "cancelled"],
        "warnings": ent.warnings + ([ent.reason] if ent.read_only else []),
        "hosted_companies": [{"id": c.id, "name": c.name} for c in hosted],
        "hosted_users": live_users(db, customer.id) if hosted else None,
        "downloads": [{"version": d.version, "result": d.result, "at": d.created_at, "user": d.username}
                      for d in db.scalars(select(DownloadEvent).where(DownloadEvent.customer_id == customer.id)
                                          .order_by(DownloadEvent.id.desc()).limit(20))],
    }


def _release_out(r: SoftwareRelease) -> dict:
    return {"id": r.id, "version": r.version, "platform": r.platform, "file_name": r.file_name,
            "file_size": r.file_size, "sha256": r.sha256, "signature": r.signature,
            "release_notes": r.release_notes, "published_at": r.published_at, "status": r.status}


@router.get("/portal/downloads")
def portal_downloads(db: DB, user: CurrentUser, customer_id: uuid.UUID | None = None):
    customer = _my_customer(db, user, customer_id)
    rels = db.scalars(select(SoftwareRelease).where(SoftwareRelease.status == "published")
                      .order_by(SoftwareRelease.published_at.desc()))
    return [_release_out(r) for r in rels if _release_allowed(db, customer, r)]


@router.get("/releases")
def list_releases(db: DB, user: CurrentUser):
    _server_only()
    rels = db.scalars(select(SoftwareRelease).where(SoftwareRelease.status == "published")
                      .order_by(SoftwareRelease.published_at.desc()))
    return [{k: v for k, v in _release_out(r).items() if k in ("id", "version", "platform", "release_notes",
                                                               "published_at")} for r in rels]


# Short-lived download links, so a large installer streams straight from the server to the browser.
_LINK_KEY = secrets.token_bytes(32)
LINK_SECONDS = 300


def _link_token(release_id: uuid.UUID, user_id: uuid.UUID, exp: int) -> str:
    msg = f"{release_id}:{user_id}:{exp}".encode()
    return f"{user_id}.{exp}.{hmac.new(_LINK_KEY, msg, hashlib.sha256).hexdigest()}"


def _log_download(db, customer, user, rel, result, request) -> None:
    db.add(DownloadEvent(customer_id=customer.id if customer else None, user_id=user.id if user else None,
                         username=user.username if user else None, release_id=rel.id if rel else None,
                         version=rel.version if rel else None, result=result, ip=_ip(request)))
    db.commit()


@router.post("/downloads/{release_id}/link")
def download_link(release_id: uuid.UUID, request: Request, db: DB, user: CurrentUser):
    rel = db.get(SoftwareRelease, release_id)
    if user.is_superadmin and rel is not None:
        customer = None
    else:
        customer = _my_customer(db, user)
        if rel is None or not _release_allowed(db, customer, rel):
            _log_download(db, customer, user, rel, "denied", request)
            raise HTTPException(404, "This download is not available for your licence")
    exp = int(utcnow().timestamp()) + LINK_SECONDS
    return {"url": f"/api/v1/downloads/{release_id}?t={_link_token(release_id, user.id, exp)}", "expires_in": LINK_SECONDS}


@router.get("/downloads/{release_id}")
def download(release_id: uuid.UUID, t: str, request: Request, db: DB):
    _server_only()
    try:
        uid, exp, sig = t.split(".")
        ok = int(exp) >= utcnow().timestamp() and hmac.compare_digest(
            sig, _link_token(release_id, uuid.UUID(uid), int(exp)).split(".")[2])
    except ValueError:
        ok = False
    rel = db.get(SoftwareRelease, release_id)
    user = db.get(User, uuid.UUID(uid)) if ok else None
    if not ok or rel is None or user is None or not user.is_active:
        raise HTTPException(403, "This download link has expired. Open the licence portal and click Download again.")
    customer = db.get(Customer, user.customer_id) if user.customer_id else None
    if not user.is_superadmin and (customer is None or not _release_allowed(db, customer, rel)):
        _log_download(db, customer, user, rel, "denied", request)
        raise HTTPException(404, "This download is not available for your licence")
    path = _release_path(rel)
    if not path.is_file():
        raise HTTPException(410, "The installer file is missing on the server. Contact Voyager.")
    _log_download(db, customer, user, rel, "ok", request)
    return FileResponse(path, filename=rel.file_name, media_type="application/octet-stream")


class RenewIn(BaseModel):
    license_id: uuid.UUID | None = None
    message: str | None = Field(None, max_length=2000)


@router.post("/licenses/renew")
def request_renewal(body: RenewIn, request: Request, db: DB, user: CurrentUser):
    """Renewal or support request from the company admin (payment is handled outside the software)."""
    customer = _my_customer(db, user)
    lic = db.get(License, body.license_id) if body.license_id else None
    if lic is not None and lic.customer_id != customer.id:
        raise HTTPException(404, "Licence not found")
    service.event(db, "renewal_requested", lic=lic, customer_id=customer.id, actor=user.username, ip=_ip(request),
                  message=body.message)
    db.commit()
    service.send_mail(settings.smtp_from, f"Renewal request: {customer.legal_name}",
                      f"{user.username} asked to renew the licence of {customer.legal_name} ({customer.code}).\n\n"
                      f"{body.message or ''}")
    return {"ok": True, "message": "Your request was sent to Voyager. We will contact you shortly."}


# ================================================================== super admin: customers

class LicenseIn(BaseModel):
    plan: Literal["trial", "monthly", "annual", "perpetual"] | None = None
    starts_at: date | None = None
    expires_at: date | None = None
    max_users: int | None = Field(None, ge=1, le=10000)
    max_installations: int | None = Field(None, ge=1, le=1000)
    max_companies: int | None = Field(None, ge=1, le=10000)
    modules: list[str] | None = None
    offline_grace_days: int | None = Field(None, ge=0, le=90)
    heartbeat_hours: int | None = Field(None, ge=1, le=168)
    min_version: str | None = Field(None, max_length=30)
    max_version: str | None = Field(None, max_length=30)
    notes: str | None = None

    @field_validator("modules")
    @classmethod
    def _modules(cls, v):
        if v is not None and (bad := set(v) - set(MODULES)):
            raise ValueError(f"unknown modules: {', '.join(sorted(bad))}")
        return v


CODE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,29}$")


class CustomerIn(BaseModel):
    code: str | None = None
    legal_name: str | None = Field(None, max_length=300)
    contact_name: str | None = Field(None, max_length=200)
    contact_email: str | None = Field(None, max_length=300)
    contact_phone: str | None = Field(None, max_length=50)
    gstin: str | None = Field(None, max_length=20)
    notes: str | None = None

    @field_validator("code")
    @classmethod
    def _code(cls, v):
        if v is not None:
            v = v.strip().upper()
            if not CODE.match(v):
                raise ValueError("2 to 30 capital letters, digits or -")
        return v


def _customer_out(db, c: Customer) -> dict:
    today = utcnow().date()
    lics = [lic for lic in c.licenses if lic.status != "cancelled"]
    current = min(lics, key=lambda lic: (effective_status(lic.status, lic.starts_at, lic.expires_at, today) != "active",
                                          -(lic.expires_at or date.max).toordinal()), default=None)
    return {
        "id": c.id, "code": c.code, "legal_name": c.legal_name, "status": c.status, "contact_name": c.contact_name,
        "contact_email": c.contact_email, "contact_phone": c.contact_phone, "gstin": c.gstin, "notes": c.notes,
        "created_at": c.created_at, "licenses": len(lics),
        "license_status": effective_status(current.status, current.starts_at, current.expires_at, today) if current else None,
        "expires_at": current.expires_at if current else None, "plan": current.plan if current else None,
        "installations": sum(1 for lic in lics for i in lic.installations if i.status == "active"),
        "admins": [{"id": u.id, "username": u.username, "display_name": u.display_name, "email": u.email,
                    "is_active": u.is_active, "has_password": bool(u.password_hash), "last_login_at": u.last_login_at}
                   for u in db.scalars(select(User).where(User.customer_id == c.id).order_by(User.username))],
        "hosted_companies": [{"id": co.id, "name": co.name}
                             for co in db.scalars(select(Company).where(Company.customer_id == c.id))],
    }


@router.get("/admin/customers")
def list_customers(db: DB, user: CurrentUser, q: str | None = None, include_archived: bool = False):
    _superadmin(user)
    stmt = select(Customer).order_by(Customer.legal_name)
    if not include_archived:
        stmt = stmt.where(Customer.status != "archived")
    if q:
        stmt = stmt.where(Customer.legal_name.ilike(f"%{q}%") | Customer.code.ilike(f"%{q}%"))
    return [_customer_out(db, c) for c in db.scalars(stmt)]


@router.get("/admin/customers/{customer_id}")
def get_customer(customer_id: uuid.UUID, db: DB, user: CurrentUser):
    _superadmin(user)
    c = db.get(Customer, customer_id)
    if c is None:
        raise HTTPException(404, "Customer not found")
    return {**_customer_out(db, c), "license_list": [_license_out(db, lic, admin=True) for lic in c.licenses]}


class AdminIn(BaseModel):
    username: str
    display_name: str | None = Field(None, max_length=200)
    email: str | None = Field(None, max_length=300)


class NewCustomerIn(CustomerIn):
    """A customer in one step: details, its licence (number of users) and its company admin (manager)."""
    license: LicenseIn | None = None
    admin: AdminIn | None = None


@router.post("/admin/customers", status_code=201)
def create_customer(body: NewCustomerIn, db: DB, user: CurrentUser):
    _superadmin(user)
    if not body.code or not body.legal_name:
        raise HTTPException(422, "Give a customer code and the legal name")
    if db.scalar(select(Customer.id).where(Customer.code == body.code)):
        raise HTTPException(409, f"Customer code {body.code} is already used")
    details = body.model_dump(exclude={"license", "admin"})
    c = Customer(**details, status="active")
    db.add(c)
    db.flush()
    audit.record(db, actor_of(user), "customer.create", "customer", c.id, after=details)
    # All or nothing: a taken admin ID or a bad licence leaves no half-made customer behind.
    lic, key = _new_license(db, user, c, body.license) if body.license else (None, None)
    admin, link = _new_admin(db, user, c, body.admin) if body.admin else (None, None)
    db.commit()
    out = _customer_out(db, c)
    if lic is not None:
        out.update(license_key=key, license_id=lic.id)
    if admin is not None:
        out.update(admin_username=admin.username, setup_link=link, emailed=_mail_invite(c, admin, link),
                   expires_in_hours=service.INVITE_HOURS)
    return out


@router.patch("/admin/customers/{customer_id}")
def update_customer(customer_id: uuid.UUID, body: CustomerIn, db: DB, user: CurrentUser):
    _superadmin(user)
    c = db.get(Customer, customer_id)
    if c is None:
        raise HTTPException(404, "Customer not found")
    changes = body.model_dump(exclude_unset=True)
    if "code" in changes and changes["code"] != c.code:
        raise HTTPException(422, "The customer code cannot be changed")
    changes.pop("code", None)
    before = {k: getattr(c, k) for k in changes}
    for k, v in changes.items():
        setattr(c, k, v)
    audit.record(db, actor_of(user), "customer.update", "customer", c.id, before=before, after=changes)
    db.commit()
    return _customer_out(db, c)


class StatusIn(BaseModel):
    status: Literal["active", "suspended", "archived"]
    reason: str | None = None


@router.post("/admin/customers/{customer_id}/status")
def customer_status(customer_id: uuid.UUID, body: StatusIn, db: DB, user: CurrentUser):
    _superadmin(user)
    c = db.get(Customer, customer_id)
    if c is None:
        raise HTTPException(404, "Customer not found")
    before = c.status
    c.status = body.status
    if body.status != "active":   # its company admins can no longer sign in to the portal
        for u in db.scalars(select(User).where(User.customer_id == c.id)):
            db.query(UserSession).filter(UserSession.user_id == u.id).delete()
    service.event(db, f"customer_{body.status}", customer_id=c.id, actor=user.username, reason=body.reason)
    audit.record(db, actor_of(user), "customer.status", "customer", c.id, before={"status": before},
                 after={"status": body.status}, reason=body.reason)
    db.commit()
    return _customer_out(db, c)


def _new_admin(db, user: User, c: Customer, body: AdminIn) -> tuple[User, str]:
    """The customer's company admin (manager), with a one-time link to set their password. Not committed."""
    from app.api.users import USERNAME, _find_user
    username = body.username.strip()
    if not USERNAME.match(username):
        raise HTTPException(422, "User ID: 3 to 50 letters, digits or . _ - @, starting with a letter or digit")
    if _find_user(db, username):
        raise HTTPException(409, "This user ID is already taken")
    u = User(username=username, display_name=body.display_name, email=body.email or c.contact_email,
             customer_id=c.id, created_by=user.username)
    db.add(u)
    db.flush()
    # The admin also manages the customer's books hosted here.
    for co in db.scalars(select(Company).where(Company.customer_id == c.id)):
        db.add(UserCompanyAccess(user_id=u.id, company_id=co.id, role="owner"))
    link = service.new_invite(db, u, user.username)
    audit.record(db, actor_of(user), "customer.admin_create", "user", u.id, after={"customer": c.code, "username": username})
    return u, link


def _mail_invite(c: Customer, u: User, link: str) -> bool:
    return service.send_mail(u.email, "Your Tally Cloud account",
                               f"Hello {u.display_name or u.username},\n\nAn account was made for you at Tally Cloud "
                               f"({c.legal_name}). User ID: {u.username}\n\nSet your password with this link (valid "
                               f"{service.INVITE_HOURS} hours, one use). You may also choose your own user ID "
                               f"there:\n{link}\n")


@router.post("/admin/customers/{customer_id}/admins", status_code=201)
def create_customer_admin(customer_id: uuid.UUID, body: AdminIn, db: DB, user: CurrentUser):
    """A company admin for the licence portal. They set their own password with a one-time link."""
    _superadmin(user)
    c = db.get(Customer, customer_id)
    if c is None:
        raise HTTPException(404, "Customer not found")
    u, link = _new_admin(db, user, c, body)
    db.commit()
    mailed = _mail_invite(c, u, link)
    return {"id": u.id, "username": u.username, "setup_link": link, "emailed": mailed,
            "expires_in_hours": service.INVITE_HOURS}


@router.post("/admin/users/{user_id}/invite")
def reissue_invite(user_id: uuid.UUID, db: DB, user: CurrentUser):
    """New one-time link: first sign-in, or a forgotten password."""
    _superadmin(user)
    u = db.get(User, user_id)
    if u is None or u.customer_id is None:
        raise HTTPException(404, "Company admin not found")
    link = service.new_invite(db, u, user.username)
    audit.record(db, actor_of(user), "user.invite", "user", u.id)
    db.commit()
    mailed = service.send_mail(u.email, "Set your Tally Cloud password",
                               f"Set your Tally Cloud password (user ID {u.username}) with this link, valid "
                               f"{service.INVITE_HOURS} hours:\n{link}\n")
    return {"setup_link": link, "emailed": mailed, "expires_in_hours": service.INVITE_HOURS}


class ActiveIn(BaseModel):
    is_active: bool


@router.post("/admin/users/{user_id}/active")
def set_admin_active(user_id: uuid.UUID, body: ActiveIn, db: DB, user: CurrentUser):
    _superadmin(user)
    u = db.get(User, user_id)
    if u is None or u.customer_id is None:
        raise HTTPException(404, "Company admin not found")
    u.is_active = body.is_active
    if not body.is_active:
        db.query(UserSession).filter(UserSession.user_id == u.id).delete()
    audit.record(db, actor_of(user), "user.update", "user", u.id, after={"is_active": body.is_active})
    db.commit()
    return {"ok": True}


class HostedIn(BaseModel):
    company_ids: list[uuid.UUID]


@router.put("/admin/customers/{customer_id}/companies")
def set_hosted_companies(customer_id: uuid.UUID, body: HostedIn, db: DB, user: CurrentUser):
    """Books hosted on this server that belong to the customer, and so follow its licence."""
    _superadmin(user)
    c = db.get(Customer, customer_id)
    if c is None:
        raise HTTPException(404, "Customer not found")
    wanted = set(body.company_ids)
    admins = list(db.scalars(select(User).where(User.customer_id == c.id)))
    for co in db.scalars(select(Company).where((Company.customer_id == c.id) | Company.id.in_(wanted))):
        if co.id in wanted:
            if co.customer_id not in (None, c.id):
                raise HTTPException(409, f"{co.name} belongs to another customer")
            co.customer_id = c.id
            for a in admins:
                if db.get(UserCompanyAccess, (a.id, co.id)) is None:
                    db.add(UserCompanyAccess(user_id=a.id, company_id=co.id, role="owner"))
        else:
            co.customer_id = None
    audit.record(db, actor_of(user), "customer.hosted", "customer", c.id, after={"companies": [str(i) for i in wanted]})
    db.commit()
    return _customer_out(db, c)


# ================================================================== super admin: licences

class NewLicenseIn(LicenseIn):
    customer_id: uuid.UUID


def _new_license(db, user: User, c: Customer, body: LicenseIn) -> tuple[License, str]:
    """A licence with its key (shown once). Not committed."""
    plan = body.plan or "annual"
    start = body.starts_at or utcnow().date()
    expires = body.expires_at if "expires_at" in body.model_fields_set else service.default_expiry(plan, start)
    if plan != "perpetual" and expires is None:
        raise HTTPException(422, "Give an expiry date (only perpetual licences have none)")
    if expires and expires < start:
        raise HTTPException(422, "The expiry date is before the start date")
    key = new_license_key()
    data = body.model_dump(include=set(LicenseIn.model_fields) - {"plan", "starts_at", "expires_at"}, exclude_none=True)
    data.setdefault("modules", list(MODULES))
    lic = License(customer_id=c.id, plan=plan, starts_at=start, expires_at=expires, key_hash=hash_license_key(key),
                  key_hint=key[-4:], status="pending", created_by=user.username, **data)
    db.add(lic)
    db.flush()
    service.event(db, "license_create", lic=lic, actor=user.username, plan=plan,
                  expires_at=expires.isoformat() if expires else None)
    audit.record(db, actor_of(user), "license.create", "license", lic.id, after={"customer": c.code, "plan": plan})
    return lic, key


@router.post("/admin/licenses", status_code=201)
def create_license(body: NewLicenseIn, db: DB, user: CurrentUser):
    _superadmin(user)
    c = db.get(Customer, body.customer_id)
    if c is None:
        raise HTTPException(404, "Customer not found")
    lic, key = _new_license(db, user, c, body)
    db.commit()
    db.refresh(lic)
    return {**_license_out(db, lic, admin=True), "license_key": key,
            "note": "The licence key is shown only now. Give it to the customer's company admin."}


@router.patch("/admin/licenses/{license_id}")
def update_license(license_id: uuid.UUID, body: LicenseIn, db: DB, user: CurrentUser):
    _superadmin(user)
    lic = db.get(License, license_id)
    if lic is None:
        raise HTTPException(404, "Licence not found")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("plan") == "perpetual" and "expires_at" not in changes:
        changes["expires_at"] = None
    before = {k: getattr(lic, k) for k in changes}
    for k, v in changes.items():
        setattr(lic, k, v)
    if lic.plan != "perpetual" and lic.expires_at is None:
        raise HTTPException(422, "Give an expiry date (only perpetual licences have none)")
    service.event(db, "license_update", lic=lic, actor=user.username,
                  changes={k: str(v) for k, v in changes.items()})
    audit.record(db, actor_of(user), "license.update", "license", lic.id, before=before, after=changes)
    db.commit()
    return _license_out(db, lic, admin=True)


class LicenseActionIn(BaseModel):
    action: Literal["activate", "suspend", "resume", "revoke", "cancel", "renew", "reissue_key"]
    expires_at: date | None = None     # renew: the new expiry date (default: one more plan period)
    reason: str | None = None


@router.post("/admin/licenses/{license_id}/action")
def license_action(license_id: uuid.UUID, body: LicenseActionIn, db: DB, user: CurrentUser):
    _superadmin(user)
    lic = db.get(License, license_id)
    if lic is None:
        raise HTTPException(404, "Licence not found")
    before = {"status": lic.status, "expires_at": lic.expires_at}
    extra = {}
    if lic.status == "revoked" and body.action != "reissue_key":
        raise HTTPException(409, "A revoked licence cannot be used again; create a new licence")
    if body.action in ("activate", "resume"):
        lic.status = "active"
    elif body.action == "suspend":
        lic.status = "suspended"
    elif body.action == "revoke":
        lic.status = "revoked"
        for i in lic.installations:
            if i.status == "active":
                i.status, i.deactivated_at = "deactivated", utcnow()
    elif body.action == "cancel":
        lic.status = "cancelled"
    elif body.action == "renew":
        if lic.plan == "perpetual":
            raise HTTPException(422, "A perpetual licence does not expire")
        base = max(lic.expires_at or utcnow().date(), utcnow().date())
        lic.expires_at = body.expires_at or service.default_expiry(lic.plan, base + timedelta(days=1))
        if lic.status in ("suspended", "cancelled", "pending"):
            lic.status = "active"
    elif body.action == "reissue_key":   # the old key stops working; activated servers keep working
        key = new_license_key()
        lic.key_hash, lic.key_hint = hash_license_key(key), key[-4:]
        extra = {"license_key": key, "note": "The new licence key is shown only now. The old key no longer works."}
    service.event(db, f"license_{body.action}", lic=lic, actor=user.username, reason=body.reason,
                  expires_at=lic.expires_at.isoformat() if lic.expires_at else None)
    audit.record(db, actor_of(user), f"license.{body.action}", "license", lic.id, before=before,
                 after={"status": lic.status, "expires_at": lic.expires_at}, reason=body.reason)
    db.commit()
    if body.action in ("suspend", "revoke", "renew"):
        service.send_mail(lic.customer.contact_email, f"Tally Cloud licence: {body.action}",
                          f"The Tally Cloud licence of {lic.customer.legal_name} was {body.action}"
                          f"{'ed' if body.action.endswith('w') else 'd'}."
                          + (f" It now runs until {lic.expires_at:%d %b %Y}." if body.action == "renew" else ""))
    return {**_license_out(db, lic, admin=True), **extra}


class InstallationActionIn(BaseModel):
    action: Literal["release", "block", "unblock"]
    reason: str | None = None


@router.post("/admin/installations/{inst_id}/action")
def installation_action(inst_id: uuid.UUID, body: InstallationActionIn, db: DB, user: CurrentUser):
    """release: frees the slot (e.g. the server was replaced); block: this server may never activate again."""
    _superadmin(user)
    inst = db.get(Installation, inst_id)
    if inst is None:
        raise HTTPException(404, "Installation not found")
    before = inst.status
    if body.action == "release":
        inst.status, inst.deactivated_at = "deactivated", utcnow()
    elif body.action == "block":
        inst.status, inst.deactivated_at = "blocked", utcnow()
    elif inst.status == "blocked":
        inst.status = "deactivated"
    service.event(db, f"installation_{body.action}", lic=inst.license, installation_id=inst.installation_id,
                  actor=user.username, reason=body.reason)
    audit.record(db, actor_of(user), f"installation.{body.action}", "installation", inst.id,
                 before={"status": before}, after={"status": inst.status}, reason=body.reason)
    db.commit()
    return _license_out(db, inst.license, admin=True)


# ================================================================== super admin: reports

@router.get("/admin/licensing/dashboard")
def licensing_dashboard(db: DB, user: CurrentUser):
    _superadmin(user)
    today = utcnow().date()
    lics = list(db.scalars(select(License)))
    by_status: dict[str, int] = {}
    expiring = []
    for lic in lics:
        st = effective_status(lic.status, lic.starts_at, lic.expires_at, today)
        by_status[st] = by_status.get(st, 0) + 1
        if st == "active" and lic.expires_at and (lic.expires_at - today).days <= 30:
            expiring.append({"license_id": lic.id, "customer": lic.customer.legal_name, "customer_id": lic.customer_id,
                             "expires_at": lic.expires_at, "days_left": (lic.expires_at - today).days})
    week = utcnow() - timedelta(days=7)
    versions = dict(db.execute(select(Installation.product_version, func.count()).where(Installation.status == "active")
                               .group_by(Installation.product_version)).all())
    return {
        "customers": db.scalar(select(func.count(Customer.id)).where(Customer.status != "archived")),
        "customers_suspended": db.scalar(select(func.count(Customer.id)).where(Customer.status == "suspended")),
        "licenses_by_status": by_status,
        "expiring_30_days": sorted(expiring, key=lambda e: e["days_left"]),
        "installations_active": db.scalar(select(func.count(Installation.id)).where(Installation.status == "active")),
        "versions": {k or "unknown": v for k, v in versions.items()},
        "activation_failures_7_days": db.scalar(select(func.count(LicenseEvent.id)).where(
            LicenseEvent.event_type == "activate", LicenseEvent.result == "rejected", LicenseEvent.created_at >= week)),
        "downloads_30_days": db.scalar(select(func.count(DownloadEvent.id)).where(
            DownloadEvent.result == "ok", DownloadEvent.created_at >= utcnow() - timedelta(days=30))),
        "renewal_requests": [
            {"customer_id": e.customer_id, "customer": (db.get(Customer, e.customer_id).legal_name if e.customer_id else None),
             "by": e.actor, "message": (e.details or {}).get("message"), "at": e.created_at}
            for e in db.scalars(select(LicenseEvent).where(LicenseEvent.event_type == "renewal_requested")
                                .order_by(LicenseEvent.id.desc()).limit(10))],
    }


@router.get("/admin/licensing/events")
def licensing_events(db: DB, user: CurrentUser, customer_id: uuid.UUID | None = None,
                     license_id: uuid.UUID | None = None, limit: int = 200):
    _superadmin(user)
    stmt = select(LicenseEvent).order_by(LicenseEvent.id.desc()).limit(min(limit, 1000))
    if customer_id:
        stmt = stmt.where(LicenseEvent.customer_id == customer_id)
    if license_id:
        stmt = stmt.where(LicenseEvent.license_id == license_id)
    return [{c.name: getattr(e, c.name) for c in LicenseEvent.__table__.columns} for e in db.scalars(stmt)]


@router.get("/admin/downloads")
def download_history(db: DB, user: CurrentUser, customer_id: uuid.UUID | None = None, limit: int = 200):
    _superadmin(user)
    stmt = select(DownloadEvent).order_by(DownloadEvent.id.desc()).limit(min(limit, 1000))
    if customer_id:
        stmt = stmt.where(DownloadEvent.customer_id == customer_id)
    return [{c.name: getattr(e, c.name) for c in DownloadEvent.__table__.columns} for e in db.scalars(stmt)]


# ================================================================== super admin: releases

def _release_dir() -> Path:
    if not settings.release_dir:
        raise HTTPException(404, "ERP_RELEASE_DIR is not set on this server")
    return Path(settings.release_dir)


def _release_path(rel: SoftwareRelease) -> Path:
    return Path(settings.release_dir or ".") / f"{rel.id}-{rel.file_name}"


@router.get("/admin/releases")
def admin_releases(db: DB, user: CurrentUser):
    _superadmin(user)
    rels = db.scalars(select(SoftwareRelease).order_by(SoftwareRelease.created_at.desc()))
    return [{**_release_out(r), "downloads": db.scalar(select(func.count(DownloadEvent.id)).where(
        DownloadEvent.release_id == r.id, DownloadEvent.result == "ok"))} for r in rels]


SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


@router.post("/admin/releases", status_code=201)
def upload_release(db: DB, user: CurrentUser, version: str = Form(..., max_length=30),
                   platform: str = Form("windows", max_length=20), release_notes: str | None = Form(None),
                   file: UploadFile = File(...)):
    _superadmin(user)
    if not re.fullmatch(r"\d+(\.\d+){0,3}", version.strip()):
        raise HTTPException(422, "Version like 1.2.0")
    if db.scalar(select(SoftwareRelease.id).where(SoftwareRelease.version == version, SoftwareRelease.platform == platform)):
        raise HTTPException(409, f"Version {version} for {platform} already exists")
    folder = _release_dir()
    folder.mkdir(parents=True, exist_ok=True)
    name = SAFE_NAME.sub("_", Path(file.filename or f"TallyCloud-{version}.exe").name)[:150]
    rel = SoftwareRelease(id=uuid.uuid4(), version=version.strip(), platform=platform, file_name=name, file_size=0,
                          sha256="", release_notes=release_notes, status="draft", created_by=user.username)
    tmp = folder / f".upload-{rel.id}"
    digest = hashlib.sha256()
    size = 0
    with tmp.open("wb") as out:
        while chunk := file.file.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
    rel.sha256, rel.file_size = digest.hexdigest(), size
    try:
        rel.signature = sign_bytes(rel.sha256.encode())    # installations can check the file came from us
    except RuntimeError:
        rel.signature = None
    os.replace(tmp, _release_path(rel))
    db.add(rel)
    audit.record(db, actor_of(user), "release.upload", "release", rel.id,
                 after={"version": rel.version, "platform": platform, "sha256": rel.sha256, "size": size})
    db.commit()
    return _release_out(rel)


class ReleaseStatusIn(BaseModel):
    status: Literal["draft", "published", "withdrawn"]
    release_notes: str | None = None


@router.post("/admin/releases/{release_id}/status")
def release_status(release_id: uuid.UUID, body: ReleaseStatusIn, db: DB, user: CurrentUser):
    _superadmin(user)
    rel = db.get(SoftwareRelease, release_id)
    if rel is None:
        raise HTTPException(404, "Release not found")
    before = rel.status
    rel.status = body.status
    if body.release_notes is not None:
        rel.release_notes = body.release_notes
    if body.status == "published" and rel.published_at is None:
        rel.published_at = utcnow()
    audit.record(db, actor_of(user), "release.status", "release", rel.id, before={"status": before},
                 after={"status": rel.status})
    db.commit()
    return _release_out(rel)


@router.get("/licensing/public-key")
def public_key():
    """The key installations use to check entitlements (also built into the installer)."""
    _server_only()
    from app.licensing.core import public_key_b64
    return {"public_key": public_key_b64(), "algorithm": "Ed25519"}


