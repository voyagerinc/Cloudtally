"""Sign-in with user ID and password, and company users managed by their company admin."""
import re
import uuid
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app import audit
from app.api.deps import DB, CurrentUser, actor_of, authorize
from app.audit import Actor
from app.core.config import settings
from app.core.security import (
    ALL_PERMS, PERM_GROUPS, REPORTS, ROLE_LABELS, ROLES, Perm, allows, check_password_rules, has_perm, hash_key,
    hash_password, new_session_token, perms_for, perms_of, verify_password,
)
from app.models import Company, User, UserCompanyAccess, UserSession
from app.models.base import utcnow

router = APIRouter(tags=["sign-in & users"])

USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{2,49}$")
# Compared against when the user ID does not exist, so a wrong ID takes as long as a wrong password.
_DUMMY_HASH = hash_password("not-a-real-password")


class LoginIn(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


class CompanyUserIn(BaseModel):
    username: str
    display_name: str | None = Field(None, max_length=200)
    password: str | None = None
    role: str = "accountant"
    permissions: list[str] | None = None     # given: a custom set instead of the role's defaults
    add_existing: bool = False                # the user ID exists: give that user access to this company


class CompanyUserPatch(BaseModel):
    display_name: str | None = Field(None, max_length=200)
    role: str | None = None
    permissions: list[str] | None = None
    password: str | None = None
    is_active: bool | None = None
    unlock: bool = False


def _find_user(db, username: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.username) == username.strip().lower()))


# ------------------------------------------------------------------ sign-in

@router.post("/auth/login")
def login(body: LoginIn, request: Request, db: DB):
    now = utcnow()
    user = _find_user(db, body.username)
    wrong = HTTPException(401, "Wrong user ID or password")
    if user is None or not user.password_hash or not user.is_active:
        verify_password(body.password, _DUMMY_HASH)
        raise wrong
    if user.locked_until and user.locked_until > now:
        raise HTTPException(401, "Too many wrong passwords. Try again after "
                                 f"{max(1, round((user.locked_until - now).total_seconds() / 60))} min, or ask your company admin.")
    if not verify_password(body.password, user.password_hash):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= settings.login_max_failures:
            user.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
            user.failed_logins = 0
        audit.record(db, Actor(user.username, "user", user.id), "user.login_failed", "user", user.id,
                     after={"client": request.client.host if request.client else None})
        db.commit()
        raise wrong
    token = new_session_token()
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
    db.add(UserSession(user_id=user.id, token_hash=hash_key(token), created_at=now, last_seen_at=now,
                       expires_at=now + timedelta(days=settings.session_max_days),
                       client=(request.headers.get("user-agent") or "")[:200]))
    audit.record(db, actor_of(user), "user.login", "user", user.id,
                 after={"client": request.client.host if request.client else None})
    db.commit()
    return {"token": token, "username": user.username, "must_change_password": user.must_change_password}


@router.post("/auth/logout", status_code=204)
def logout(request: Request, db: DB, user: CurrentUser):
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        db.execute(delete(UserSession).where(UserSession.token_hash == hash_key(auth[7:].strip())))
        db.commit()


@router.post("/auth/password")
def change_password(body: PasswordIn, request: Request, db: DB, user: CurrentUser):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(422, "The current password is wrong")
    if body.new_password == body.current_password:
        raise HTTPException(422, "Choose a password different from the current one")
    if reason := check_password_rules(body.new_password, user.username):
        raise HTTPException(422, reason)
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    # Sign out every other browser of this user.
    auth = request.headers.get("authorization") or ""
    keep = hash_key(auth[7:].strip()) if auth.lower().startswith("bearer ") else ""
    db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.token_hash != keep))
    audit.record(db, actor_of(user), "user.password_change", "user", user.id)
    db.commit()
    return {"ok": True}


@router.get("/me")
def me(db: DB, user: CurrentUser):
    companies = [{"company_id": a.company_id, "role": a.role, "role_label": ROLE_LABELS.get(a.role, a.role),
                  "permissions": sorted(perms_of(a))} for a in user.access]
    return {"id": user.id, "username": user.username, "display_name": user.display_name,
            "is_superadmin": user.is_superadmin, "must_change_password": user.must_change_password,
            "has_password": bool(user.password_hash),
            "all_permissions": sorted(ALL_PERMS) if user.is_superadmin else None,
            "companies": companies}


@router.get("/permissions")
def permission_catalog(user: CurrentUser):
    """Everything the user screen offers: the permission groups, the reports and the ready-made roles."""
    return {
        "groups": [{"name": name, "permissions": [{"key": k, "label": v} for k, v in perms]} for name, perms in PERM_GROUPS],
        "reports": [{"key": k, "label": v, "permission": Perm.report(k)} for k, v in REPORTS.items()],
        "roles": [{"key": k, "label": ROLE_LABELS.get(k, k), "permissions": sorted(v)} for k, v in ROLES.items()
                  if k != "integration"],
    }


# ------------------------------------------------------------------ company users

def _not_manageable(db, actor: User, target: User) -> str | None:
    """Why the actor may not change this person's sign-in (password, name, active), or None.

    A company admin may only do that for people who work in no other company than the ones they manage,
    so the admin of one company cannot take over a user of another."""
    if actor.is_superadmin:
        return None
    if target.is_superadmin:
        return "This user is a super admin."
    for a in target.access:
        if not has_perm(db, actor, a.company_id, Perm.USERS_MANAGE):
            return "This user also works in a company you do not manage. Ask a super admin."
    return None


def _permissions(db, actor: User, company_id: uuid.UUID, role: str, permissions: list[str] | None) -> list[str] | None:
    """Validated custom permission list to store (None = the role's defaults)."""
    if role != "custom" and role not in ROLES:
        raise HTTPException(422, f"Unknown role {role!r}")
    if role == "custom" and permissions is None:
        raise HTTPException(422, "Choose the permissions for a custom role")
    wanted = set(permissions) if permissions is not None else ROLES[role]
    if unknown := wanted - ALL_PERMS:
        raise HTTPException(422, f"Unknown permissions: {', '.join(sorted(unknown))}")
    mine = perms_for(db, actor, company_id)
    if beyond := sorted(p for p in wanted if not allows(mine, p)):
        raise HTTPException(403, f"You cannot give permissions you do not have yourself: {', '.join(beyond)}")
    if permissions is None or (role in ROLES and wanted == ROLES[role]):
        return None
    return sorted(wanted)


def _out(u: User, access: UserCompanyAccess) -> dict:
    now = utcnow()
    return {
        "id": u.id, "username": u.username, "display_name": u.display_name,
        "role": access.role, "role_label": ROLE_LABELS.get(access.role, access.role),
        "custom": access.permissions is not None, "permissions": sorted(perms_of(access)),
        "is_active": u.is_active, "is_superadmin": u.is_superadmin, "has_password": bool(u.password_hash),
        "must_change_password": u.must_change_password, "locked": bool(u.locked_until and u.locked_until > now),
        "last_login_at": u.last_login_at, "created_at": u.created_at, "created_by": u.created_by,
        "other_companies": len(u.access) - 1,
    }


def _access(db, company_id: uuid.UUID, user_id: uuid.UUID) -> tuple[User, UserCompanyAccess]:
    access = db.get(UserCompanyAccess, (user_id, company_id))
    if access is None:
        raise HTTPException(404, "User not found in this company")
    return access.user, access


@router.get("/companies/{company_id}/users")
def list_company_users(company_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.USERS_MANAGE)
    rows = db.scalars(select(UserCompanyAccess).join(User).where(UserCompanyAccess.company_id == company_id)
                      .order_by(func.lower(User.username)))
    return [_out(a.user, a) for a in rows]


@router.post("/companies/{company_id}/users", status_code=201)
def create_company_user(company_id: uuid.UUID, body: CompanyUserIn, db: DB, user: CurrentUser):
    company: Company = authorize(db, user, company_id, Perm.USERS_MANAGE)
    username = body.username.strip()
    perms = _permissions(db, user, company_id, body.role, body.permissions)
    existing = _find_user(db, username)
    if existing is not None:
        if not body.add_existing:
            raise HTTPException(409, "This user ID is already taken. Choose another, or add the existing user.")
        if db.get(UserCompanyAccess, (existing.id, company_id)):
            raise HTTPException(409, f"{existing.username} already has access to {company.name}")
        if reason := _not_manageable(db, user, existing):
            raise HTTPException(403, reason)
        target = existing
        target.is_active = True      # e.g. a user switched off when removed from their last company
    else:
        if not USERNAME.match(username):
            raise HTTPException(422, "User ID: 3 to 50 letters, digits or . _ - @, starting with a letter or digit")
        if not body.password:
            raise HTTPException(422, "Give the user a first password")
        if reason := check_password_rules(body.password, username):
            raise HTTPException(422, reason)
        target = User(username=username, display_name=body.display_name, password_hash=hash_password(body.password),
                      must_change_password=True, created_by=user.username)
        db.add(target)
        db.flush()
        audit.record(db, actor_of(user), "user.create", "user", target.id, company_id=company_id,
                     after={"username": username, "display_name": body.display_name})
    access = UserCompanyAccess(user_id=target.id, company_id=company_id, role=body.role, permissions=perms)
    db.add(access)
    audit.record(db, actor_of(user), "user.access", "user", target.id, company_id=company_id,
                 after={"role": body.role, "permissions": perms})
    db.commit()
    db.refresh(target)
    return _out(target, access)


@router.patch("/companies/{company_id}/users/{user_id}")
def update_company_user(company_id: uuid.UUID, user_id: uuid.UUID, body: CompanyUserPatch, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.USERS_MANAGE)
    target, access = _access(db, company_id, user_id)
    fields = body.model_fields_set
    is_self = target.id == user.id
    before = _out(target, access)

    if "role" in fields or "permissions" in fields:
        if is_self:
            raise HTTPException(403, "You cannot change your own permissions. Ask another admin.")
        role = body.role or access.role
        access.permissions = _permissions(db, user, company_id, role,
                                          body.permissions if "permissions" in fields else access.permissions)
        access.role = role

    profile = {f for f in ("display_name", "password", "is_active") if f in fields} | ({"unlock"} if body.unlock else set())
    if profile and not is_self:
        if reason := _not_manageable(db, user, target):
            raise HTTPException(403, reason)
    if "display_name" in fields:
        target.display_name = body.display_name
    if "password" in fields and body.password:
        if is_self:
            raise HTTPException(422, "Change your own password with Change password")
        if reason := check_password_rules(body.password, target.username):
            raise HTTPException(422, reason)
        target.password_hash = hash_password(body.password)
        target.must_change_password = True
        target.failed_logins, target.locked_until = 0, None
        db.execute(delete(UserSession).where(UserSession.user_id == target.id))
    if "is_active" in fields and body.is_active is not None:
        if is_self and not body.is_active:
            raise HTTPException(403, "You cannot switch off your own user")
        target.is_active = body.is_active
        if not body.is_active:
            db.execute(delete(UserSession).where(UserSession.user_id == target.id))
    if body.unlock:
        target.failed_logins, target.locked_until = 0, None

    after = _out(target, access)
    changed = {k: after[k] for k in after if after[k] != before[k]}
    audit.record(db, actor_of(user), "user.update", "user", target.id, company_id=company_id,
                 before={k: before[k] for k in changed}, after=changed | ({"password": "reset"} if body.password else {}))
    db.commit()
    return after


@router.delete("/companies/{company_id}/users/{user_id}", status_code=204)
def remove_company_user(company_id: uuid.UUID, user_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.USERS_MANAGE)
    target, access = _access(db, company_id, user_id)
    if target.id == user.id:
        raise HTTPException(403, "You cannot remove yourself. Ask another admin.")
    db.delete(access)
    db.flush()
    db.refresh(target)
    if not target.access and not target.is_superadmin:
        # Nobody may use a user without any company: switch it off and sign it out.
        target.is_active = False
        db.execute(delete(UserSession).where(UserSession.user_id == target.id))
    audit.record(db, actor_of(user), "user.revoke", "user", target.id, company_id=company_id)
    db.commit()
