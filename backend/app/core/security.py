"""API-key authentication and role-based permissions (PRD §4, §5.5)."""
import hashlib
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User, UserCompanyAccess


class Perm:
    COMPANY_MANAGE = "company.manage"
    USERS_MANAGE = "users.manage"
    MASTERS_READ = "masters.read"
    MASTERS_WRITE = "masters.write"
    VOUCHER_READ = "voucher.read"
    VOUCHER_WRITE = "voucher.write"
    VOUCHER_POST = "voucher.post"
    VOUCHER_APPROVE = "voucher.approve"
    VOUCHER_DELETE = "voucher.delete"
    PERIOD_LOCK = "period.lock"
    REPORTS_READ = "reports.read"
    EXPORT = "export"
    AUDIT_READ = "audit.read"
    SYNC_READ = "sync.read"
    SYNC_MANAGE = "sync.manage"
    CONFLICT_RESOLVE = "conflict.resolve"
    INTEGRATION = "integration"


_READ = {Perm.MASTERS_READ, Perm.VOUCHER_READ, Perm.REPORTS_READ}
ROLES: dict[str, set[str]] = {
    "owner": {v for k, v in vars(Perm).items() if k.isupper()},
    "accounts_admin": _READ | {
        Perm.MASTERS_WRITE, Perm.VOUCHER_WRITE, Perm.VOUCHER_POST, Perm.VOUCHER_APPROVE, Perm.VOUCHER_DELETE,
        Perm.PERIOD_LOCK, Perm.EXPORT, Perm.AUDIT_READ, Perm.SYNC_READ, Perm.SYNC_MANAGE, Perm.CONFLICT_RESOLVE,
    },
    "accountant": _READ | {Perm.VOUCHER_WRITE, Perm.VOUCHER_POST, Perm.EXPORT, Perm.SYNC_READ},
    "sales_purchase": _READ | {Perm.VOUCHER_WRITE},
    "inventory": _READ | {Perm.VOUCHER_WRITE},
    "approver": _READ | {Perm.VOUCHER_APPROVE, Perm.VOUCHER_POST},
    "auditor": _READ | {Perm.AUDIT_READ, Perm.SYNC_READ, Perm.EXPORT},
    "integration": _READ | {Perm.INTEGRATION, Perm.VOUCHER_WRITE, Perm.VOUCHER_POST, Perm.SYNC_READ},
}


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return "erp_" + secrets.token_urlsafe(32)


def user_for_key(db: Session, api_key: str) -> User | None:
    return db.scalar(select(User).where(User.api_key_hash == hash_key(api_key), User.is_active.is_(True)))


def role_for(db: Session, user: User, company_id: uuid.UUID) -> str | None:
    if user.is_superadmin:
        return "owner"
    access = db.get(UserCompanyAccess, (user.id, company_id))
    return access.role if access else None


def has_perm(db: Session, user: User, company_id: uuid.UUID, perm: str) -> bool:
    role = role_for(db, user, company_id)
    return bool(role) and perm in ROLES.get(role, set())


def allowed_company_ids(db: Session, user: User) -> list[uuid.UUID] | None:
    """None means unrestricted (super admin)."""
    if user.is_superadmin:
        return None
    return list(db.scalars(select(UserCompanyAccess.company_id).where(UserCompanyAccess.user_id == user.id)))
