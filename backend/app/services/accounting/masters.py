"""Company setup and accounting masters (PRD §5.1, §5.2)."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import audit
from app.audit import Actor
from app.models import (
    Company, FinancialYear, Ledger, LedgerGroup, OutboxStatus, SyncOutbox, TallyConnector, VoucherType,
)
from app.models.base import utcnow
from app.models.masters import BASE_VOUCHER_TYPES
from app.services.accounting.vouchers import VoucherError

# Tally's predefined chart: (name, parent, nature, is_revenue, affects_gross_profit)
DEFAULT_GROUPS = [
    ("Capital Account", None, "liability", False, False),
    ("Loans (Liability)", None, "liability", False, False),
    ("Current Liabilities", None, "liability", False, False),
    ("Branch / Divisions", None, "liability", False, False),
    ("Suspense A/c", None, "liability", False, False),
    ("Fixed Assets", None, "asset", False, False),
    ("Investments", None, "asset", False, False),
    ("Current Assets", None, "asset", False, False),
    ("Misc. Expenses (ASSET)", None, "asset", False, False),
    ("Sales Accounts", None, "income", True, True),
    ("Purchase Accounts", None, "expense", True, True),
    ("Direct Incomes", None, "income", True, True),
    ("Direct Expenses", None, "expense", True, True),
    ("Indirect Incomes", None, "income", True, False),
    ("Indirect Expenses", None, "expense", True, False),
    ("Reserves & Surplus", "Capital Account", "liability", False, False),
    ("Bank OD A/c", "Loans (Liability)", "liability", False, False),
    ("Secured Loans", "Loans (Liability)", "liability", False, False),
    ("Unsecured Loans", "Loans (Liability)", "liability", False, False),
    ("Duties & Taxes", "Current Liabilities", "liability", False, False),
    ("Provisions", "Current Liabilities", "liability", False, False),
    ("Sundry Creditors", "Current Liabilities", "liability", False, False),
    ("Bank Accounts", "Current Assets", "asset", False, False),
    ("Cash-in-Hand", "Current Assets", "asset", False, False),
    ("Deposits (Asset)", "Current Assets", "asset", False, False),
    ("Loans & Advances (Asset)", "Current Assets", "asset", False, False),
    ("Stock-in-Hand", "Current Assets", "asset", False, False),
    ("Sundry Debtors", "Current Assets", "asset", False, False),
]


def fy_bounds(start: date) -> tuple[date, date]:
    end = date(start.year + 1, start.month, start.day) - timedelta(days=1)
    return start, end


def fy_start_for(d: date, start_month: int = 4) -> date:
    year = d.year if d.month >= start_month else d.year - 1
    return date(year, start_month, 1)


def ensure_financial_year(db: Session, company: Company, d: date) -> FinancialYear:
    start, end = fy_bounds(fy_start_for(d))
    fy = db.scalar(select(FinancialYear).where(FinancialYear.company_id == company.id, FinancialYear.start_date == start))
    if fy is None:
        fy = FinancialYear(company_id=company.id, start_date=start, end_date=end)
        db.add(fy)
        db.flush()
    return fy


def create_company(db: Session, data: dict, actor: Actor, *, with_defaults: bool = True) -> Company:
    if db.scalar(select(Company.id).where(Company.name == data["name"])):
        raise VoucherError("duplicate", f"Company '{data['name']}' already exists")
    company = Company(**data)
    db.add(company)
    db.flush()
    ensure_financial_year(db, company, company.books_from)
    if with_defaults:
        groups: dict[str, LedgerGroup] = {}
        for name, parent, nature, revenue, gp in DEFAULT_GROUPS:
            g = LedgerGroup(company_id=company.id, name=name, parent=groups.get(parent), nature=nature,
                            is_revenue=revenue, affects_gross_profit=gp, is_reserved=True)
            db.add(g)
            groups[name] = g
        db.add(Ledger(company_id=company.id, name="Cash", group=groups["Cash-in-Hand"]))
        for base in BASE_VOUCHER_TYPES:
            db.add(VoucherType(company_id=company.id, name=base, base_type=base))
    audit.record(db, actor, "company.create", "company", company.id, company_id=company.id, after=data)
    db.flush()
    return company


def group_by_name(db: Session, company_id: uuid.UUID, name: str) -> LedgerGroup:
    g = db.scalar(select(LedgerGroup).where(LedgerGroup.company_id == company_id, LedgerGroup.name == name))
    if g is None:
        raise VoucherError("missing_master", f"Group '{name}' does not exist")
    return g


def create_group(db: Session, company: Company, data: dict, actor: Actor) -> LedgerGroup:
    parent = group_by_name(db, company.id, data["parent"]) if data.get("parent") else None
    nature = data.get("nature") or (parent.nature if parent else None)
    if nature is None:
        raise VoucherError("validation_failure", "A primary group needs a nature")
    g = LedgerGroup(company_id=company.id, name=data["name"], parent=parent, nature=nature,
                    is_revenue=data.get("is_revenue", parent.is_revenue if parent else nature in ("income", "expense")),
                    affects_gross_profit=data.get("affects_gross_profit", parent.affects_gross_profit if parent else False))
    db.add(g)
    audit.record(db, actor, "group.create", "group", None, company_id=company.id, after=data)
    db.flush()
    return g


LEDGER_FIELDS = ("opening_balance", "is_billwise", "credit_days", "gstin", "pan", "state", "gst_registration_type",
                 "tax_type", "gst_duty_head", "bank_account_no", "bank_ifsc", "mailing_name", "address", "is_active",
                 "pincode", "country", "place_of_supply", "contact_person", "phone", "mobile", "fax", "email",
                 "email_cc", "website", "contacts", "bank_name", "bank_branch", "bank_account_holder", "party_banks",
                 "msme_type", "msme_udyam_no", "msme_activity", "msme_from")


def ledger_doc(led: Ledger) -> dict:
    return {"name": led.name, "group": led.group.name, **{f: getattr(led, f) for f in LEDGER_FIELDS}}


def _enqueue_ledger(db: Session, led: Ledger, actor: Actor) -> None:
    if not db.get(TallyConnector, led.company_id):
        return
    db.execute(update(SyncOutbox).where(
        SyncOutbox.entity_type == "ledger", SyncOutbox.entity_id == led.id,
        SyncOutbox.status.in_([OutboxStatus.PENDING, OutboxStatus.FAILED]),
    ).values(status=OutboxStatus.SUPERSEDED, completed_at=utcnow()))
    db.add(SyncOutbox(company_id=led.company_id, entity_type="ledger", entity_id=led.id, operation="upsert",
                      entity_version=led.version, idempotency_key=f"ledger:{led.id}:v{led.version}",
                      voucher_number=led.name, created_by=actor.name, correlation_id=audit.correlation_id.get()))


def _check_ledger(led: Ledger, changed) -> None:
    """Checks applied when a ledger is saved. Only fields changed in this save are checked, so old data
    imported from Tally (which does not enforce these) never blocks an unrelated edit."""
    if {"gstin", "pan"} & set(changed) and led.gstin and led.pan and led.gstin[2:12] != led.pan:
        raise VoucherError("invalid", f"GSTIN {led.gstin} does not contain the PAN {led.pan}")
    if {"credit_days", "is_billwise"} & set(changed) and led.credit_days and not led.is_billwise:
        raise VoucherError("invalid", "A credit period needs bill-wise details switched on")


def _name_free(db: Session, company_id, name: str, ledger_id=None) -> None:
    clash = db.scalar(select(Ledger.id).where(Ledger.company_id == company_id, func.lower(Ledger.name) == name.lower()))
    if clash and clash != ledger_id:
        raise VoucherError("duplicate", f"Ledger '{name}' already exists")


def create_ledger(db: Session, company: Company, data: dict, actor: Actor, *, source: str = "erp") -> Ledger:
    _name_free(db, company.id, data["name"])
    led = Ledger(company_id=company.id, name=data["name"], group=group_by_name(db, company.id, data["group"]),
                 source_system=source, **{f: data[f] for f in LEDGER_FIELDS if data.get(f) is not None})
    if led.opening_balance is None:
        led.opening_balance = Decimal("0")
    _check_ledger(led, data)
    db.add(led)
    db.flush()
    audit.record(db, actor, "ledger.create", "ledger", led.id, company_id=company.id, after=ledger_doc(led))
    if source == "erp":
        _enqueue_ledger(db, led, actor)
    return led


def update_ledger(db: Session, led: Ledger, data: dict, actor: Actor, *, source: str = "erp") -> Ledger:
    before = ledger_doc(led)
    if data.get("name") and data["name"] != led.name:
        _name_free(db, led.company_id, data["name"], led.id)
        led.name = data["name"]
    if data.get("group"):
        led.group = group_by_name(db, led.company_id, data["group"])
    for f in LEDGER_FIELDS:
        if f in data:   # an explicit null clears the field
            setattr(led, f, data[f])
    if led.opening_balance is None:
        led.opening_balance = Decimal("0")
    _check_ledger(led, data)
    led.version += 1
    db.flush()
    audit.record(db, actor, "ledger.update", "ledger", led.id, company_id=led.company_id,
                 before=before, after=ledger_doc(led))
    if source == "erp":
        _enqueue_ledger(db, led, actor)
    return led
