"""Sign-in (user ID + password sessions, API keys for integrations) and permissions (PRD §4, §5.5)."""
import hashlib
import hmac
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
    REPORTS_READ = "reports.read"       # every report; "report.<key>" grants a single one
    EXPORT = "export"
    AUDIT_READ = "audit.read"
    SYNC_READ = "sync.read"
    SYNC_MANAGE = "sync.manage"
    CONFLICT_RESOLVE = "conflict.resolve"
    INTEGRATION = "integration"

    @staticmethod
    def report(key: str) -> str:
        return f"report.{key}"


# Reports that can be granted one by one (key -> label shown to the company admin).
REPORTS: dict[str, str] = {
    "trial-balance": "Trial Balance",
    "profit-loss": "Profit & Loss",
    "balance-sheet": "Balance Sheet",
    "day-book": "Day Book",
    "ledger": "Ledger statement",
    "stock-summary": "Stock Summary",
    "receivables": "Receivables (ageing)",
    "payables": "Payables (ageing)",
    "sales-register": "Sales Register",
    "purchase-register": "Purchase Register",
    "sales-analysis": "Sales analysis",
    "purchase-analysis": "Purchase analysis",
    "fifo-costing": "FIFO stock costing",
    "sales-rm-cost": "Sales with raw-material cost",
}

# What the company admin sees when ticking permissions, grouped by area.
PERM_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Vouchers", [(Perm.VOUCHER_READ, "View vouchers"), (Perm.VOUCHER_WRITE, "Create and alter vouchers"),
                  (Perm.VOUCHER_POST, "Post vouchers"), (Perm.VOUCHER_APPROVE, "Approve vouchers"),
                  (Perm.VOUCHER_DELETE, "Cancel vouchers")]),
    ("Masters", [(Perm.MASTERS_READ, "View ledgers and stock items"),
                 (Perm.MASTERS_WRITE, "Create and alter ledgers and stock items")]),
    ("Reports", [(Perm.REPORTS_READ, "All reports")] + [(Perm.report(k), v) for k, v in REPORTS.items()]
                + [(Perm.EXPORT, "Export to CSV")]),
    # Running sync and changing the Tally connection stay with the company admin (manager): no tick for it.
    ("Tally sync", [(Perm.SYNC_READ, "View sync status"), (Perm.CONFLICT_RESOLVE, "Resolve conflicts")]),
    ("Administration", [(Perm.USERS_MANAGE, "Manage users and permissions"),
                        (Perm.COMPANY_MANAGE, "Company settings"), (Perm.PERIOD_LOCK, "Lock periods"),
                        (Perm.AUDIT_READ, "View the audit trail")]),
]
ALL_PERMS: set[str] = {p for _, perms in PERM_GROUPS for p, _ in perms} | {Perm.INTEGRATION, Perm.SYNC_MANAGE}
# Only the company admin (manager) role runs Tally sync and changes the Tally connection.
MANAGER_ONLY = {Perm.SYNC_MANAGE}

_READ = {Perm.MASTERS_READ, Perm.VOUCHER_READ, Perm.REPORTS_READ}
ROLES: dict[str, set[str]] = {
    "owner": {v for k, v in vars(Perm).items() if k.isupper()},
    "accounts_admin": _READ | {
        Perm.MASTERS_WRITE, Perm.VOUCHER_WRITE, Perm.VOUCHER_POST, Perm.VOUCHER_APPROVE, Perm.VOUCHER_DELETE,
        Perm.PERIOD_LOCK, Perm.EXPORT, Perm.AUDIT_READ, Perm.SYNC_READ, Perm.CONFLICT_RESOLVE,
    },
    "accountant": _READ | {Perm.VOUCHER_WRITE, Perm.VOUCHER_POST, Perm.EXPORT, Perm.SYNC_READ},
    "sales_purchase": _READ | {Perm.VOUCHER_WRITE},
    "inventory": _READ | {Perm.VOUCHER_WRITE},
    "approver": _READ | {Perm.VOUCHER_APPROVE, Perm.VOUCHER_POST},
    "auditor": _READ | {Perm.AUDIT_READ, Perm.SYNC_READ, Perm.EXPORT},
    "reports_viewer": {Perm.REPORTS_READ},
    "integration": _READ | {Perm.INTEGRATION, Perm.VOUCHER_WRITE, Perm.VOUCHER_POST, Perm.SYNC_READ},
}
ROLE_LABELS: dict[str, str] = {
    "owner": "Company admin", "accounts_admin": "Accounts admin", "accountant": "Accountant",
    "sales_purchase": "Sales & purchase", "inventory": "Inventory", "approver": "Approver",
    "auditor": "Auditor", "reports_viewer": "Reports only", "integration": "Integration",
    "custom": "Custom",
}


# ------------------------------------------------------------------ API keys (integrations)

def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return "erp_" + secrets.token_urlsafe(32)


def user_for_key(db: Session, api_key: str) -> User | None:
    return db.scalar(select(User).where(User.api_key_hash == hash_key(api_key), User.is_active.is_(True)))


# ------------------------------------------------------------------ passwords and sessions

MIN_PASSWORD = 8
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def check_password_rules(password: str, username: str | None = None) -> str | None:
    """The reason a password is not acceptable, or None."""
    if len(password) < MIN_PASSWORD:
        return f"Password must have at least {MIN_PASSWORD} characters"
    if len(password) > 200:
        return "Password is too long"
    if username and password.strip().lower() == username.strip().lower():
        return "Password must not be the same as the user ID"
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), dklen=len(bytes.fromhex(digest)),
                             n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def new_session_token() -> str:
    return "ses_" + secrets.token_urlsafe(32)


# ------------------------------------------------------------------ permissions

def role_for(db: Session, user: User, company_id: uuid.UUID) -> str | None:
    if user.is_superadmin:
        return "owner"
    access = db.get(UserCompanyAccess, (user.id, company_id))
    return access.role if access else None


def perms_of(access: UserCompanyAccess) -> set[str]:
    """A custom permission list replaces the role's defaults."""
    perms = set(access.permissions) & ALL_PERMS if access.permissions is not None else set(ROLES.get(access.role, set()))
    return perms if access.role == "owner" else perms - MANAGER_ONLY


def perms_for(db: Session, user: User, company_id: uuid.UUID) -> set[str]:
    if user.is_superadmin:
        return set(ALL_PERMS)
    access = db.get(UserCompanyAccess, (user.id, company_id))
    return perms_of(access) if access else set()


def allows(perms: set[str], perm: str) -> bool:
    return perm in perms or (perm.startswith("report.") and Perm.REPORTS_READ in perms)


def has_perm(db: Session, user: User, company_id: uuid.UUID, perm: str) -> bool:
    return allows(perms_for(db, user, company_id), perm)


def allowed_company_ids(db: Session, user: User) -> list[uuid.UUID] | None:
    """None means unrestricted (super admin)."""
    if user.is_superadmin:
        return None
    return list(db.scalars(select(UserCompanyAccess.company_id).where(UserCompanyAccess.user_id == user.id)))
