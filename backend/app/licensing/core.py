"""Licence rules shared by the cloud (issues entitlements) and installations (enforce them).

An entitlement is a JSON document signed with the cloud's Ed25519 private key. Installations hold only
the public key, so they can check an entitlement but never make one."""
import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.core.config import settings
from app.core.security import Perm

# ------------------------------------------------------------------ modules and what they unlock

MODULES: dict[str, str] = {
    "accounting": "Vouchers and ledgers",
    "inventory": "Inventory (stock items and stock reports)",
    "reports": "Reports",
    "tally_sync": "Tally sync",
    "export": "Export to CSV",
}
ALL_MODULES = list(MODULES)


def module_of(perm: str) -> str | None:
    """The licensed module a permission belongs to (None: always available, e.g. user management)."""
    if perm in (Perm.REPORTS_READ,) or perm.startswith("report."):
        return "inventory" if perm == Perm.report("stock-summary") else "reports"
    if perm.startswith(("voucher.", "masters.")):
        return "accounting"
    if perm.startswith("sync.") or perm == Perm.CONFLICT_RESOLVE:
        return "tally_sync"
    if perm == Perm.EXPORT:
        return "export"
    return None


# Not allowed while the software is read-only (expired, suspended, or offline past the grace period).
# Viewing, reports, export and user management keep working: customers keep access to their data.
WRITE_PERMS = {
    Perm.MASTERS_WRITE, Perm.VOUCHER_WRITE, Perm.VOUCHER_POST, Perm.VOUCHER_APPROVE, Perm.VOUCHER_DELETE,
    Perm.SYNC_MANAGE, Perm.CONFLICT_RESOLVE, Perm.PERIOD_LOCK, Perm.COMPANY_MANAGE, Perm.INTEGRATION,
}

PLANS = ("trial", "monthly", "annual", "perpetual")
ADMIN_STATUSES = ("pending", "active", "suspended", "revoked", "cancelled")
EXPIRY_WARN_DAYS = (30, 15, 7, 1)


def new_license_key() -> str:
    """e.g. TCL-7KQ2-M9XD-4RTA-PW3H: easy to read out on the phone, 100 bits of randomness."""
    abc = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(abc) for _ in range(20))
    return "TCL-" + "-".join(raw[i:i + 4] for i in range(0, 20, 4))


def normalise_key(key: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", key.upper())


def hash_license_key(key: str) -> str:
    return hashlib.sha256(normalise_key(key).encode()).hexdigest()


def version_tuple(v: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))[:4]


def version_allowed(version: str | None, min_version: str | None, max_version: str | None) -> bool:
    """min 1.2 allows 1.2.0 and later; max 1.4 allows every 1.4.x (compared to the precision given)."""
    v = version_tuple(version)
    if min_version and v + (0,) * 4 < version_tuple(min_version) + (0,) * 4:
        return False
    if max_version:
        mx = version_tuple(max_version)
        if v[:len(mx)] > mx:
            return False
    return True


def effective_status(status: str, starts_at: date | None, expires_at: date | None, today: date) -> str:
    """The admin status plus what the dates say: pending/active become expired after the expiry date."""
    if status in ("suspended", "revoked", "cancelled"):
        return status
    if expires_at and today > expires_at:
        return "expired"
    if starts_at and today < starts_at:
        return "pending"
    return status


# ------------------------------------------------------------------ signing

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def generate_keypair() -> tuple[bytes, str]:
    """(private key PEM, public key base64) for `python -m app.cli license-keygen`."""
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return pem, base64.b64encode(pub).decode()


@lru_cache(maxsize=1)
def _private_key() -> Ed25519PrivateKey:
    if not settings.license_signing_key_file:
        raise RuntimeError("ERP_LICENSE_SIGNING_KEY_FILE is not set; run: python -m app.cli license-keygen")
    with open(settings.license_signing_key_file, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def public_key_b64() -> str:
    pub = _private_key().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(pub).decode()


def sign(payload: dict) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode()
    return f"{_b64(body)}.{_b64(_private_key().sign(body))}"


def sign_bytes(data: bytes) -> str:
    return _b64(_private_key().sign(data))


def verify(token: str, public_key: str | None) -> dict | None:
    """The payload of a token signed by the cloud, or None when it is not genuine."""
    if not token or not public_key:
        return None
    try:
        body, sig = token.split(".")
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key)).verify(_unb64(sig), _unb64(body))
        return json.loads(_unb64(body))
    except (ValueError, InvalidSignature, TypeError):
        return None


# ------------------------------------------------------------------ the entitlement an installation works with

@dataclass
class Entitlement:
    state: str                          # unlicensed | active | grace | read_only | not_activated
    reason: str = ""
    modules: set[str] | None = None     # None: every module
    max_users: int | None = None
    max_companies: int | None = None
    read_only: bool = False
    warnings: list[str] = field(default_factory=list)
    expires_at: str | None = None
    customer: str | None = None
    plan: str | None = None

    def allows_module(self, module: str | None) -> bool:
        return module is None or self.modules is None or module in self.modules

    def public(self) -> dict:
        return {"state": self.state, "reason": self.reason, "read_only": self.read_only,
                "modules": sorted(self.modules) if self.modules is not None else None,
                "max_users": self.max_users, "max_companies": self.max_companies, "warnings": self.warnings,
                "expires_at": self.expires_at, "customer": self.customer, "plan": self.plan}


FULL = Entitlement(state="unlicensed")      # licensing off, or the owner's own books on the cloud server


def expiry_warning(expires_at: date | None, today: date) -> str | None:
    if not expires_at:
        return None
    left = (expires_at - today).days
    if 0 <= left <= EXPIRY_WARN_DAYS[0]:
        return (f"The licence expires {'today' if left == 0 else f'in {left} day' + ('s' if left > 1 else '')}"
                f" ({expires_at:%d %b %Y}). Renew it to keep working without interruption.")
    return None


def entitlement_from_license(lic, today: date) -> Entitlement:
    """Hosted books on the cloud server: the customer's licence, read live from the database."""
    status = effective_status(lic.status, lic.starts_at, lic.expires_at, today)
    ent = Entitlement(state="active", modules=set(lic.modules or []), max_users=lic.max_users,
                      max_companies=lic.max_companies, expires_at=lic.expires_at.isoformat() if lic.expires_at else None,
                      customer=lic.customer.legal_name if lic.customer else None, plan=lic.plan)
    return _apply_status(ent, status, lic.expires_at, today)


def _apply_status(ent: Entitlement, status: str, expires_at: date | None, today: date) -> Entitlement:
    messages = {
        "expired": "The licence has expired.",
        "suspended": "The licence is suspended.",
        "revoked": "The licence has been revoked.",
        "cancelled": "The licence has been cancelled.",
        "pending": "The licence has not started yet.",
    }
    if status in messages:
        ent.state, ent.read_only = "read_only", True
        ent.reason = messages[status] + " The software is read-only: you can view, run reports and export, " \
                                        "but not enter or change data. Contact Voyager to renew."
    elif w := expiry_warning(expires_at, today):
        ent.warnings.append(w)
    return ent


def entitlement_from_token(payload: dict, installation_id: str, last_check_at: datetime | None,
                           now: datetime) -> Entitlement:
    """Installation: the last signed entitlement, aged by the offline grace period."""
    if payload.get("installation_id") != installation_id:
        return Entitlement(state="not_activated", read_only=True,
                           reason="The licence on file belongs to another installation. Activate this server again.")
    today = now.date()
    expires = date.fromisoformat(payload["expires_at"]) if payload.get("expires_at") else None
    starts = date.fromisoformat(payload["starts_at"]) if payload.get("starts_at") else None
    ent = Entitlement(state="active", modules=set(payload.get("modules") or []), max_users=payload.get("max_users"),
                      max_companies=payload.get("max_companies"), expires_at=payload.get("expires_at"),
                      customer=payload.get("customer_name"), plan=payload.get("plan"))
    ent = _apply_status(ent, effective_status(payload.get("status", "active"), starts, expires, today), expires, today)
    if ent.read_only:
        return ent
    grace = timedelta(days=int(payload.get("offline_grace_days") or 7))
    heartbeat = timedelta(hours=int(payload.get("heartbeat_hours") or 24))
    since = now - (last_check_at or now)
    if since > grace:
        ent.state, ent.read_only = "read_only", True
        ent.reason = (f"This server could not reach the Tally Cloud licence service for {since.days} days "
                      f"(allowed: {grace.days}). The software is read-only until it connects again: "
                      "check the internet connection, then Licence → Check now.")
    elif since > heartbeat * 2:
        ent.state = "grace"
        left = grace - since
        ent.warnings.append(f"The licence service has not been reachable for {since.days or 1} day(s). "
                            f"Everything keeps working for {max(left.days, 0)} more day(s); please check the "
                            "internet connection.")
    return ent


def utc(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
