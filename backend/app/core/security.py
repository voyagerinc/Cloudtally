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
    VOUCHER_CREATE = "voucher.create"
    VOUCHER_ALTER = "voucher.alter"
    VOUCHER_POST = "voucher.post"
    VOUCHER_APPROVE = "voucher.approve"
    VOUCHER_DELETE = "voucher.delete"
    PERIOD_LOCK = "period.lock"
    REPORTS_READ = "reports.read"       # every report; "report.<key>" grants a single one
    EXPORT = "export"
    AUDIT_READ = "audit.read"
    SYNC_READ = "sync.read"
    SYNC_MANAGE = "sync.manage"         # Tally connector settings (company admin)
    SYNC_PULL = "sync.pull"             # Pull: read vouchers and masters from Tally into the software
    SYNC_PUSH = "sync.push"             # Push: write vouchers made in the software into Tally
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
    "cash-flow": "Cash Flow",
    "funds-flow": "Funds Flow",
    "day-book": "Day Book",
    "ledger": "Ledger statement",
    "stock-summary": "Stock Summary",
    "receivables": "Receivables (ageing)",
    "receivables-eway": "Receivables (ageing by e-way bill date)",
    "receivables-eway-monthly": "Receivables monthly (by e-way bill date)",
    "payables": "Payables (ageing)",
    "sales-register": "Sales Register",
    "purchase-register": "Purchase Register",
    "sales-analysis": "Sales analysis",
    "purchase-analysis": "Purchase analysis",
    "fifo-costing": "FIFO stock costing",
    "fifo-monthly": "FIFO stock costing month by month",
    "godown-summary": "Store-wise and item-wise stock summary",
    "sales-rm-cost": "Sales with raw-material cost",
    "sales-fifo-cost": "Sales register with FIFO cost",
    "sales-group-wise": "Item group-wise sales",
    "sales-group-units": "Item group-wise sales (main & alternate unit, sale and FIFO cost rates)",
    "sales-item-units": "Item-wise sales with FIFO cost (main & alternate unit)",
    "stock-journal": "Stock journal register (in / out)",
    "material-transfer-wip": "Material transfer to WIP",
    "material-transfer-wip-main": "Material transfer to WIP (main group)",
    "wo-manufacturing": "WO manufacturing & manufacturing journal",
    "gstr1": "GST: GSTR-1 (outward supplies)",
    "gstr2": "GST: GSTR-2 (inward supplies)",
    "hsn-summary": "GST: HSN summary",
    "einvoice": "GST: E-invoice status",
    "eway-bill": "GST: E-way bill status",
    "custom-fields": "Custom fields from Tally (e.g. chassis / motor no.)",
    "mis-pl": "MIS: P&L by division",
    "mis-monthly-sheet": "MIS: Monthly finance sheet (P&L)",
    "mis-finance-sheet": "Coolace MIS: Monthly finance sheet (P&L)",
    "mis-finance-sheet-coolace": "Coolace: Monthly finance sheet (P&L)",
    "mis-expense": "MIS: Expenses by division",
    "mis-exp-breakup": "MIS: Expense breakup",
    "mis-deferred": "MIS: Deferred expenses",
}

# What the company admin sees when ticking permissions, grouped by area.
PERM_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Vouchers", [(Perm.VOUCHER_READ, "View vouchers"), (Perm.VOUCHER_CREATE, "Create vouchers"),
                  (Perm.VOUCHER_ALTER, "Alter vouchers"),
                  (Perm.VOUCHER_DELETE, "Delete vouchers (made in the software, with remarks)"),
                  (Perm.VOUCHER_POST, "Post and cancel vouchers"), (Perm.VOUCHER_APPROVE, "Approve vouchers")]),
    ("Masters", [(Perm.MASTERS_READ, "View ledgers and stock items"),
                 (Perm.MASTERS_WRITE, "Create and alter ledgers and stock items")]),
    ("Reports", [(Perm.REPORTS_READ, "All reports")] + [(Perm.report(k), v) for k, v in REPORTS.items()]
                + [(Perm.EXPORT, "Export to CSV")]),
    # Changing the Tally connection stays with the company admin (manager): no tick for it.
    ("Tally sync", [(Perm.SYNC_READ, "View sync status, history and vouchers pending push"),
                    (Perm.SYNC_PULL, "Pull from Tally (Tally to software)"),
                    (Perm.SYNC_PUSH, "Push to Tally (software to Tally)"),
                    (Perm.CONFLICT_RESOLVE, "Resolve conflicts")]),
    ("Administration", [(Perm.USERS_MANAGE, "Manage users and permissions"),
                        (Perm.COMPANY_MANAGE, "Company settings"), (Perm.PERIOD_LOCK, "Lock periods"),
                        (Perm.AUDIT_READ, "View the audit trail")]),
]
# Older permission keys, read as the ones that replaced them ("create and alter" became two ticks).
LEGACY_PERMS: dict[str, set[str]] = {"voucher.write": {Perm.VOUCHER_CREATE, Perm.VOUCHER_ALTER}}


def upgrade_perms(perms) -> set[str]:
    out = set()
    for p in perms:
        out |= LEGACY_PERMS.get(p, {p})
    return out


ALL_PERMS: set[str] = {p for _, perms in PERM_GROUPS for p, _ in perms} | {Perm.INTEGRATION, Perm.SYNC_MANAGE}
# Only the company admin (manager) role runs Tally sync and changes the Tally connection.
MANAGER_ONLY = {Perm.SYNC_MANAGE}

_READ = {Perm.MASTERS_READ, Perm.VOUCHER_READ, Perm.REPORTS_READ}
_WRITE = {Perm.VOUCHER_CREATE, Perm.VOUCHER_ALTER}
ROLES: dict[str, set[str]] = {
    "owner": {v for k, v in vars(Perm).items() if k.isupper()},
    "accounts_admin": _READ | {
        Perm.MASTERS_WRITE, *_WRITE, Perm.VOUCHER_POST, Perm.VOUCHER_APPROVE, Perm.VOUCHER_DELETE,
        Perm.PERIOD_LOCK, Perm.EXPORT, Perm.AUDIT_READ, Perm.SYNC_READ, Perm.CONFLICT_RESOLVE,
    },
    "accountant": _READ | _WRITE | {Perm.VOUCHER_POST, Perm.EXPORT, Perm.SYNC_READ},
    "sales_purchase": _READ | _WRITE,
    "inventory": _READ | _WRITE,
    "approver": _READ | {Perm.VOUCHER_APPROVE, Perm.VOUCHER_POST},
    "auditor": _READ | {Perm.AUDIT_READ, Perm.SYNC_READ, Perm.EXPORT},
    "reports_viewer": {Perm.REPORTS_READ},
    "integration": _READ | _WRITE | {Perm.INTEGRATION, Perm.VOUCHER_POST, Perm.SYNC_READ},
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
    perms = upgrade_perms(access.permissions) & ALL_PERMS if access.permissions is not None else set(ROLES.get(access.role, set()))
    if access.role != "owner":
        return perms - MANAGER_ONLY
    if Perm.SYNC_MANAGE in perms:       # the company admin ran sync before Pull and Push were two ticks
        perms |= {Perm.SYNC_PULL, Perm.SYNC_PUSH}
    return perms


def perms_for(db: Session, user: User, company_id: uuid.UUID) -> set[str]:
    if user.is_superadmin:
        return set(ALL_PERMS)
    access = db.get(UserCompanyAccess, (user.id, company_id))
    return perms_of(access) if access else set()


def allows(perms: set[str], perm: str) -> bool:
    return perm in perms or (perm.startswith("report.") and Perm.REPORTS_READ in perms)


def has_perm(db: Session, user: User, company_id: uuid.UUID, perm: str) -> bool:
    return allows(perms_for(db, user, company_id), perm)


# ------------------------------------------------------------------ voucher type name-wise rights
# On top of the voucher ticks: a user may be limited to some voucher types, and to some actions on each.
VT_ACTIONS: dict[str, str] = {"read": "View", "create": "Create", "alter": "Alter", "delete": "Delete"}
PERM_ACTION: dict[str, str] = {Perm.VOUCHER_READ: "read", Perm.VOUCHER_CREATE: "create",
                               Perm.VOUCHER_ALTER: "alter", Perm.VOUCHER_DELETE: "delete"}


def voucher_rights_for(db: Session, user: User, company_id: uuid.UUID) -> dict | None:
    """{voucher type name: [actions]}, or None when every voucher type is allowed."""
    if user.is_superadmin:
        return None
    access = db.get(UserCompanyAccess, (user.id, company_id))
    return access.voucher_rights if access else {}


def type_allows(rights: dict | None, type_name: str, action: str) -> bool:
    return rights is None or action in (rights.get(type_name) or ())


def readable_types(rights: dict | None) -> list[str] | None:
    return None if rights is None else [t for t, acts in rights.items() if "read" in acts]


def allowed_company_ids(db: Session, user: User) -> list[uuid.UUID] | None:
    """None means unrestricted (super admin)."""
    if user.is_superadmin:
        return None
    return list(db.scalars(select(UserCompanyAccess.company_id).where(UserCompanyAccess.user_id == user.id)))
