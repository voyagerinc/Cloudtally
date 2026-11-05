"""This software's own licence: shown to every user (banner), managed by the super admin (Licence tab)."""
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import __version__, audit
from app.api.deps import DB, CurrentUser, actor_of
from app.core.config import settings
from app.core.security import check_password_rules, hash_password
from app.licensing import client, service
from app.licensing.enforce import entitlement_for, seats_in_use
from app.models import Company, User

router = APIRouter(prefix="/licence", tags=["licence"])


@router.get("")
def licence_status(db: DB, user: CurrentUser, company_id: uuid.UUID | None = None):
    if company_id is not None and not user.is_superadmin and all(a.company_id != company_id for a in user.access):
        company_id = None
    ent = entitlement_for(db, company_id)
    out = {"mode": settings.license_mode, "version": __version__, **ent.public(),
           "users_used": seats_in_use(db, company_id) if ent.max_users is not None else None}
    if settings.license_mode == "client" and user.is_superadmin:
        st = client.state(db)
        out.update(installation_id=st.installation_id, server_url=st.server_url or settings.license_server_url,
                   activated=bool(st.installation_secret), last_check_at=st.last_check_at,
                   last_attempt_at=st.last_attempt_at, last_error=st.last_error, update_available=st.update_available,
                   companies_used=db.query(Company).count(),
                   public_key_set=bool(settings.license_public_key))
    return out


def _client_admin(user) -> None:
    if settings.license_mode != "client":
        raise HTTPException(404, "This server does not use an installation licence")
    if not user.is_superadmin:
        raise HTTPException(403, "Only the super admin of this server can manage its licence")


class ActivateIn(BaseModel):
    license_key: str = Field(max_length=60)
    server_label: str | None = Field(None, max_length=200)
    server_url: str | None = Field(None, max_length=300)


@router.post("/activate")
def activate(body: ActivateIn, db: DB, user: CurrentUser):
    _client_admin(user)
    if not settings.license_public_key:
        raise HTTPException(422, "ERP_LICENSE_PUBLIC_KEY is not set on this server (the installer sets it)")
    try:
        client.activate(db, body.license_key, body.server_label, body.server_url)
    except client.LicenceError as exc:
        raise HTTPException(422, str(exc))
    audit.record(db, actor_of(user), "licence.activate", "licence", None, after={"server_label": body.server_label})
    db.commit()
    return licence_status(db, user)


@router.post("/check")
def check_now(db: DB, user: CurrentUser):
    _client_admin(user)
    try:
        client.check(db, "validate")
    except client.LicenceError as exc:
        raise HTTPException(422, str(exc))
    return licence_status(db, user)


@router.post("/deactivate")
def deactivate(db: DB, user: CurrentUser):
    _client_admin(user)
    try:
        client.deactivate(db)
    except client.LicenceError as exc:
        raise HTTPException(422, f"{exc}. The server slot was not released; ask Voyager to release it.")
    audit.record(db, actor_of(user), "licence.deactivate", "licence", None)
    db.commit()
    return licence_status(db, user)


# ------------------------------------------------------------------ first start of a new installation

def _first_run_needed(db) -> bool:
    return settings.license_mode == "client" and not db.query(User).filter(User.password_hash.is_not(None)).count()


@router.get("/first-run")
def first_run_status(db: DB):
    """Asked by the page before anyone can sign in: is this a new installation without a manager yet?"""
    needed = _first_run_needed(db)
    return {"needed": needed, "server_url": settings.license_server_url if needed else None}


class FirstRunIn(BaseModel):
    license_key: str = Field(max_length=60)
    server_label: str | None = Field(None, max_length=200)
    server_url: str | None = Field(None, max_length=300)
    username: str = Field(max_length=50)
    display_name: str | None = Field(None, max_length=200)
    password: str = Field(max_length=200)


@router.post("/first-run", status_code=201)
def first_run(body: FirstRunIn, request: Request, db: DB):
    """Activates this server with the licence key and creates its manager (the customer's company admin).

    Only possible while the installation has no user yet, and only with a valid licence key."""
    if not _first_run_needed(db):
        raise HTTPException(409, "This server is already set up. Sign in instead.")
    service.setup_limit.check(f"first-run:{request.client.host if request.client else ''}")
    from app.api.users import USERNAME
    username = body.username.strip()
    if not USERNAME.match(username):
        raise HTTPException(422, "User ID: 3 to 50 letters, digits or . _ - @, starting with a letter or digit")
    if reason := check_password_rules(body.password, username):
        raise HTTPException(422, reason)
    if not settings.license_public_key:
        raise HTTPException(422, "ERP_LICENSE_PUBLIC_KEY is not set on this server (the installer sets it)")
    try:
        client.activate(db, body.license_key, body.server_label, body.server_url)
    except client.LicenceError as exc:
        raise HTTPException(422, str(exc))
    manager = User(username=username, display_name=body.display_name, password_hash=hash_password(body.password),
                   is_superadmin=True, created_by="first-run")
    db.add(manager)
    db.flush()
    audit.record(db, actor_of(manager), "licence.first_run", "user", manager.id,
                 after={"username": username, "server_label": body.server_label})
    db.commit()
    return {"username": username}
