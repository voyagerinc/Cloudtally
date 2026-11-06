"""The single voucher engine used by every voucher type and both sync directions (PRD §5.3)."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import audit
from app.audit import Actor
from app.core.config import settings
from app.models import (
    BankAllocation, BillAllocation, Company, CostAllocation, FinancialYear, Godown, Ledger, OutboxStatus,
    StockItem, SyncOutbox, SyncStatus, TallyConnector, TallyMapping, Voucher, VoucherEntry,
    VoucherInventoryLine, VoucherStatus, VoucherType, VoucherVersion,
)
from app.models.base import utcnow
from app.services.canonical import content_hash, m, q

ZERO = Decimal("0")
S = VoucherStatus


class VoucherError(Exception):
    status_code = 422

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class VersionConflict(VoucherError):
    status_code = 409


class NotAllowed(VoucherError):
    status_code = 409


# ------------------------------------------------------------------ canonical doc

def _d(value) -> str | None:
    return value.isoformat() if value else None


def to_doc(v: Voucher) -> dict:
    """Full canonical representation (names, not ids) used for API output, hashing and sync."""
    return {
        "voucher_type": v.voucher_type.name,
        "voucher_number": v.voucher_number,
        "date": _d(v.voucher_date),
        "effective_date": _d(v.effective_date),
        "reference_no": v.reference_no,
        "reference_date": _d(v.reference_date),
        "party_ledger": v.party_ledger.name if v.party_ledger else None,
        "narration": v.narration or "",
        "currency": v.currency,
        "exchange_rate": str(v.exchange_rate),
        "place_of_supply": v.place_of_supply,
        "is_invoice": v.is_invoice,
        "is_reverse_charge": v.is_reverse_charge,
        "is_cancelled": v.status == S.CANCELLED,
        "statutory": v.statutory or {},
        "entries": [
            {
                "ledger": e.ledger.name,
                "amount": m(e.amount),
                "narration": e.narration,
                "gst": e.gst or {},
                "bills": [{"type": b.ref_type, "name": b.name, "amount": m(b.amount), "due_date": _d(b.due_date)}
                          for b in e.bill_allocations],
                "cost": [{"category": c.category, "centre": c.cost_centre, "amount": m(c.amount)}
                         for c in e.cost_allocations],
                "bank": [{"transaction_type": b.transaction_type, "instrument_no": b.instrument_no,
                          "instrument_date": _d(b.instrument_date), "bank_name": b.bank_name,
                          "amount": m(b.amount)} for b in e.bank_allocations],
            }
            for e in v.entries
        ],
        "inventory": [
            {"item": i.stock_item.name, "quantity": q(i.quantity), "unit": i.unit, "rate": str(i.rate),
             "discount_pct": str(i.discount_pct), "amount": m(i.amount), "godown": i.godown, "batch": i.batch,
             "ledger": i.ledger.name if i.ledger else None,
             **({"direction": "in" if i.inward else "out"} if i.quantity == 0 else {})}
            for i in v.inventory_lines
        ],
    }


def voucher_total(v: Voucher) -> Decimal:
    return sum((e.amount for e in v.entries if e.amount > 0), ZERO)


# ------------------------------------------------------------------ lookups

def _date(value) -> date | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value))


class _Resolver:
    def __init__(self, db: Session, company_id: uuid.UUID):
        self.db, self.company_id = db, company_id
        self._ledgers: dict[str, Ledger] = {}
        self._items: dict[str, StockItem] = {}

    def ledger(self, name: str | None) -> Ledger | None:
        if not name:
            return None
        if name not in self._ledgers:
            led = self.db.scalar(select(Ledger).where(Ledger.company_id == self.company_id, Ledger.name == name))
            if led is None:
                raise VoucherError("missing_master", f"Ledger '{name}' does not exist")
            if not led.is_active:
                raise VoucherError("validation_failure", f"Ledger '{name}' is disabled")
            self._ledgers[name] = led
        return self._ledgers[name]

    def item(self, name: str) -> StockItem:
        if name not in self._items:
            item = self.db.scalar(select(StockItem).where(StockItem.company_id == self.company_id, StockItem.name == name))
            if item is None:
                raise VoucherError("missing_master", f"Stock item '{name}' does not exist")
            self._items[name] = item
        return self._items[name]

    def godown(self, name: str | None) -> None:
        # "Main Location" is Tally's built-in default godown; it exists even when not exported as a master.
        if name and name != "Main Location" and not self.db.scalar(select(Godown.id).where(Godown.company_id == self.company_id, Godown.name == name)):
            raise VoucherError("missing_master", f"Godown '{name}' does not exist")

    def voucher_type(self, name: str) -> VoucherType:
        vt = self.db.scalar(select(VoucherType).where(VoucherType.company_id == self.company_id, VoucherType.name == name))
        if vt is None:
            raise VoucherError("missing_master", f"Voucher type '{name}' does not exist")
        if not vt.is_active:
            raise VoucherError("validation_failure", f"Voucher type '{name}' is disabled")
        return vt


MAX_YEARS_AHEAD = 5   # a voucher dated further ahead is a typo, not a reason to open years


def _open_years_up_to(db: Session, company: Company, d: date) -> FinancialYear | None:
    """Like Tally: a voucher after the last financial year opens the next year(s), each starting the day
    after the previous one ends (so the company's April-March pattern continues)."""
    last = db.scalar(select(FinancialYear).where(FinancialYear.company_id == company.id)
                     .order_by(FinancialYear.end_date.desc()).limit(1))
    if last is None or d <= last.end_date:
        return None
    fy = None
    for _ in range(MAX_YEARS_AHEAD):
        start = last.end_date + timedelta(days=1)
        try:
            end = start.replace(year=start.year + 1) - timedelta(days=1)
        except ValueError:                    # 29 February
            end = start.replace(year=start.year + 1, day=28)
        fy = FinancialYear(company_id=company.id, start_date=start, end_date=end)
        db.add(fy)
        db.flush()
        if d <= end:
            return fy
        last = fy
    raise VoucherError("period_locked", f"{d} is more than {MAX_YEARS_AHEAD} years after the last financial year")


def financial_year_for(db: Session, company: Company, d: date) -> FinancialYear:
    fy = db.scalar(select(FinancialYear).where(
        FinancialYear.company_id == company.id, FinancialYear.start_date <= d, FinancialYear.end_date >= d))
    if fy is None:
        fy = _open_years_up_to(db, company, d)
    if fy is None:
        raise VoucherError("period_locked", f"No financial year covers {d}")
    if d < company.books_from:
        raise VoucherError("period_locked", f"{d} is before books beginning {company.books_from}")
    if fy.is_locked:
        raise VoucherError("period_locked", f"Financial year {fy.start_date}..{fy.end_date} is locked")
    if fy.locked_upto and d <= fy.locked_upto:
        raise VoucherError("period_locked", f"Period up to {fy.locked_upto} is locked")
    return fy


# ------------------------------------------------------------------ validation + apply

def _validate_and_build(db: Session, company: Company, data: dict, r: _Resolver, vt: VoucherType):
    entries_in = data.get("entries") or []
    inventory_in = data.get("inventory") or []
    # Like Tally. Vouchers that post to the books need balanced ledger entries, or only stock lines
    # (stock and manufacturing journals). Non-accounting vouchers (orders, material in/out, gate passes)
    # never post, so a single ledger line naming the party is fine and nothing has to balance.
    # An order or memo with nothing on it yet (Tally saves those) touches neither books nor stock.
    if not entries_in and not inventory_in and (vt.affects_accounts or vt.affects_stock):
        raise VoucherError("validation_failure", "A voucher needs ledger entries or stock lines")
    if vt.affects_accounts and len(entries_in) == 1:
        raise VoucherError("validation_failure", "A voucher needs at least two ledger entries, or only stock lines")

    entries = []
    total = ZERO
    for idx, e in enumerate(entries_in, 1):
        amount = Decimal(str(e["amount"])).quantize(Decimal("0.01"))
        if amount == ZERO:
            raise VoucherError("validation_failure", f"Line {idx}: amount cannot be zero")
        ledger = r.ledger(e["ledger"])
        bills = e.get("bills") or []
        if bills and sum(Decimal(str(b["amount"])) for b in bills) != amount:
            raise VoucherError("validation_failure", f"Line {idx}: bill allocations do not add up to {amount}")
        cost = e.get("cost") or []
        if cost and sum(Decimal(str(c["amount"])) for c in cost) != amount:
            raise VoucherError("validation_failure", f"Line {idx}: cost-centre allocations do not add up to {amount}")
        total += amount
        entries.append((idx, ledger, amount, e))
    if total != ZERO and vt.affects_accounts:
        raise VoucherError("validation_failure", f"Debit and credit totals differ by {total}")

    inventory = []
    alloc: dict[str, Decimal] = {}
    for idx, i in enumerate(inventory_in, 1):
        item = r.item(i["item"])
        r.godown(i.get("godown"))
        qty = Decimal(str(i["quantity"]))
        # Quantity 0 is a value-only line (a rate or value adjustment); its direction must then be given.
        if qty == ZERO and i.get("direction") not in ("in", "out"):
            raise VoucherError("validation_failure",
                               f"Inventory line {idx}: a line with quantity 0 needs direction 'in' or 'out'")
        inward = qty > 0 if qty != ZERO else i["direction"] == "in"
        rate = Decimal(str(i.get("rate") or 0))
        disc = Decimal(str(i.get("discount_pct") or 0))
        amount = i.get("amount")
        amount = (abs(Decimal(str(amount))) if amount not in (None, "")
                  else (abs(qty) * rate * (1 - disc / 100))).quantize(Decimal("0.01"))
        ledger = r.ledger(i.get("ledger"))
        if ledger:
            alloc[ledger.name] = alloc.get(ledger.name, ZERO) + (amount if inward else -amount)
        inventory.append((idx, item, qty, rate, disc, amount, ledger, i))

    for name, value in alloc.items() if vt.affects_accounts else ():
        booked = sum((a for _, led, a, _ in entries if led.name == name), ZERO)
        if booked != value:
            raise VoucherError("validation_failure",
                               f"Inventory allocated to '{name}' is {value} but ledger entries total {booked}")
    return entries, inventory


def _check_duplicate(db: Session, company_id, vt_id, fy_id, number: str, exclude_id=None) -> None:
    stmt = select(Voucher.id).where(
        Voucher.company_id == company_id, Voucher.voucher_type_id == vt_id, Voucher.financial_year_id == fy_id,
        Voucher.voucher_number == number, Voucher.status != S.DELETED,
    )
    if exclude_id:
        stmt = stmt.where(Voucher.id != exclude_id)
    if db.scalar(stmt.limit(1)):
        raise VoucherError("duplicate_voucher", f"Voucher number {number} already exists for this type and year")


def _next_number(db: Session, vt: VoucherType, fy_id) -> str:
    db.execute(select(VoucherType.id).where(VoucherType.id == vt.id).with_for_update())
    db.refresh(vt)
    while True:   # numbers can also arrive from Tally, so skip any already taken
        number = f"{vt.prefix}{vt.next_number}{vt.suffix}"
        vt.next_number += 1
        if not db.scalar(select(Voucher.id).where(
                Voucher.voucher_type_id == vt.id, Voucher.financial_year_id == fy_id,
                Voucher.voucher_number == number, Voucher.status != S.DELETED).limit(1)):
            return number


def _note_external_number(vt: VoucherType, number: str) -> None:
    """Keep ERP auto-numbering ahead of numbers entered in Tally for the same series."""
    if vt.numbering != "auto" or not number.startswith(vt.prefix) or not number.endswith(vt.suffix):
        return
    core = number[len(vt.prefix):len(number) - len(vt.suffix) if vt.suffix else None]
    if core.isdigit() and int(core) >= vt.next_number:
        vt.next_number = int(core) + 1


def _check_stock(db: Session, company_id, inventory, exclude_voucher_id=None) -> None:
    if settings.allow_negative_stock:
        return
    for idx, item, qty, *_ in inventory:
        if qty >= 0:
            continue
        stmt = (select(func.coalesce(func.sum(VoucherInventoryLine.quantity), 0))
                .join(Voucher).where(Voucher.company_id == company_id, Voucher.status == S.POSTED,
                                     VoucherInventoryLine.stock_item_id == item.id))
        if exclude_voucher_id:
            stmt = stmt.where(Voucher.id != exclude_voucher_id)
        available = item.opening_qty + db.scalar(stmt)
        if available + qty < 0:
            raise VoucherError("validation_failure", f"Inventory line {idx}: only {available} of '{item.name}' available")


def _apply(v: Voucher, data: dict, r: _Resolver, entries, inventory) -> None:
    v.voucher_date = _date(data["date"])
    v.effective_date = _date(data.get("effective_date"))
    v.reference_no = data.get("reference_no")
    v.reference_date = _date(data.get("reference_date"))
    v.party_ledger = r.ledger(data.get("party_ledger"))
    v.narration = data.get("narration")
    v.currency = data.get("currency") or "INR"
    v.exchange_rate = Decimal(str(data.get("exchange_rate") or 1))
    v.place_of_supply = data.get("place_of_supply")
    v.is_reverse_charge = bool(data.get("is_reverse_charge"))
    v.is_invoice = bool(inventory) or bool(data.get("is_invoice"))
    v.statutory = data.get("statutory") or {}

    v.entries.clear()
    v.inventory_lines.clear()
    for idx, ledger, amount, e in entries:
        entry = VoucherEntry(line_no=idx, ledger=ledger, amount=amount, narration=e.get("narration"), gst=e.get("gst") or {})
        entry.bill_allocations = [
            BillAllocation(ref_type=b.get("type") or "New Ref", name=b.get("name"), amount=Decimal(str(b["amount"])),
                           due_date=_date(b.get("due_date")))
            for b in e.get("bills") or []
        ]
        entry.cost_allocations = [
            CostAllocation(category=c.get("category") or "Primary Cost Category", cost_centre=c["centre"],
                           amount=Decimal(str(c["amount"])))
            for c in e.get("cost") or []
        ]
        entry.bank_allocations = [
            BankAllocation(transaction_type=b.get("transaction_type"), instrument_no=b.get("instrument_no"),
                           instrument_date=_date(b.get("instrument_date")), bank_name=b.get("bank_name"),
                           amount=Decimal(str(b["amount"])))
            for b in e.get("bank") or []
        ]
        v.entries.append(entry)
    for idx, item, qty, rate, disc, amount, ledger, i in inventory:
        v.inventory_lines.append(VoucherInventoryLine(
            line_no=idx, stock_item=item, quantity=qty, unit=i.get("unit") or item.unit, rate=rate,
            discount_pct=disc, amount=amount, godown=i.get("godown"), batch=i.get("batch"), ledger=ledger,
            inward=(i["direction"] == "in") if qty == ZERO else None,
        ))


# ------------------------------------------------------------------ versions, audit, outbox

def _record_version(db: Session, v: Voucher, actor: Actor, source: str, reason: str | None,
                    action: str, before: dict | None) -> dict:
    db.flush()
    db.refresh(v)
    doc = to_doc(v)
    v.content_hash = content_hash(doc)
    db.add(VoucherVersion(voucher_id=v.id, version=v.version, status=v.status, snapshot=doc,
                          content_hash=v.content_hash, changed_by=actor.name, source_system=source, reason=reason))
    audit.record(db, actor, f"voucher.{action}", "voucher", v.id, company_id=v.company_id,
                 before=before, after={"status": v.status, "version": v.version, **doc}, reason=reason)
    return doc


def enqueue_outbound(db: Session, v: Voucher, actor: Actor, operation: str) -> SyncOutbox | None:
    """Write the outbox event in the caller's transaction (transactional outbox, PRD §6.4)."""
    if not db.get(TallyConnector, v.company_id):
        return None
    db.execute(
        update(SyncOutbox)
        .where(SyncOutbox.entity_type == "voucher", SyncOutbox.entity_id == v.id,
               SyncOutbox.status.in_([OutboxStatus.PENDING, OutboxStatus.FAILED]))
        .values(status=OutboxStatus.SUPERSEDED, completed_at=utcnow())
    )
    event = SyncOutbox(
        company_id=v.company_id, entity_type="voucher", entity_id=v.id, operation=operation,
        entity_version=v.version, idempotency_key=f"voucher:{v.id}:v{v.version}:{operation}",
        voucher_type=v.voucher_type.name, voucher_number=v.voucher_number,
        correlation_id=audit.correlation_id.get(), created_by=actor.name,
    )
    db.add(event)
    v.sync_status = SyncStatus.PENDING
    return event


def _needs_sync(v: Voucher) -> bool:
    return v.status == S.POSTED


def _was_synced(db: Session, v: Voucher) -> bool:
    return db.scalar(select(TallyMapping.id).where(
        TallyMapping.entity_type == "voucher", TallyMapping.erp_id == v.id)) is not None


# ------------------------------------------------------------------ public API

def create_voucher(db: Session, company: Company, data: dict, actor: Actor, *, action: str = "draft",
                   source: str = "erp", reason: str | None = None, initial_status: str | None = None) -> Voucher:
    r = _Resolver(db, company.id)
    vt = r.voucher_type(data["voucher_type"])
    vdate = _date(data["date"])
    fy = financial_year_for(db, company, vdate)
    entries, inventory = _validate_and_build(db, company, data, r, vt)
    _check_stock(db, company.id, inventory)

    number = (data.get("voucher_number") or "").strip()
    if not number and source == "erp":   # Tally vouchers may legitimately have no number
        if vt.numbering == "manual":
            raise VoucherError("validation_failure", f"Voucher type '{vt.name}' uses manual numbering; give voucher_number")
        number = _next_number(db, vt, fy.id)
    if source == "erp":
        _check_duplicate(db, company.id, vt.id, fy.id, number)
    else:
        _note_external_number(vt, number)

    v = Voucher(company_id=company.id, financial_year_id=fy.id, voucher_type=vt, voucher_number=number,
                source_system=source, version=1, created_by=actor.name, updated_by=actor.name, status=S.DRAFT)
    db.add(v)
    _apply(v, data, r, entries, inventory)
    if initial_status:
        v.status = initial_status
        if initial_status == S.POSTED:
            v.posted_at = utcnow()
    else:
        _advance(v, vt, action, actor)
    _record_version(db, v, actor, source, reason, "create", None)
    if source == "erp" and _needs_sync(v):
        enqueue_outbound(db, v, actor, "upsert")
    return v


def _advance(v: Voucher, vt: VoucherType, action: str, actor: Actor) -> None:
    """Initial state for a new voucher, honouring maker-checker rules."""
    if action == "draft":
        v.status = S.DRAFT
    elif action == "submit" or (action == "post" and _requires_approval(v, vt)):
        v.status = S.PENDING_APPROVAL
    elif action == "post":
        v.status = S.POSTED
        v.posted_at = utcnow()
    else:
        raise VoucherError("validation_failure", f"Unknown action {action}")


def _requires_approval(v: Voucher, vt: VoucherType) -> bool:
    if not vt.requires_approval:
        return False
    if vt.approval_threshold is None:
        return True
    return voucher_total(v) >= vt.approval_threshold


def update_voucher(db: Session, v: Voucher, data: dict, actor: Actor, *, expected_version: int | None,
                   source: str = "erp", reason: str | None = None) -> Voucher:
    if expected_version is not None and expected_version != v.version:
        raise VersionConflict("version_conflict",
                              f"Voucher was changed by someone else (now version {v.version}, you edited {expected_version})")
    if v.status in (S.CANCELLED, S.DELETED):
        raise NotAllowed("validation_failure", f"A {v.status} voucher cannot be edited")
    company = db.get(Company, v.company_id)
    r = _Resolver(db, company.id)
    vt = r.voucher_type(data.get("voucher_type") or v.voucher_type.name)
    if vt.id != v.voucher_type_id and source == "erp" and v.status == S.POSTED and _was_synced(db, v):
        raise NotAllowed("validation_failure", "Voucher type of a synchronized voucher cannot be changed; cancel and re-enter")
    vdate = _date(data["date"])
    # Both the old and the new date must be in open periods.
    financial_year_for(db, company, v.voucher_date)
    fy = financial_year_for(db, company, vdate)
    entries, inventory = _validate_and_build(db, company, data, r, vt)
    _check_stock(db, company.id, inventory, exclude_voucher_id=v.id)
    number = (data.get("voucher_number") if source == "tally" else data.get("voucher_number") or v.voucher_number) or ""
    number = number.strip()
    renumbered = (number, vt.id, fy.id) != (v.voucher_number, v.voucher_type_id, v.financial_year_id)
    if source == "erp" and renumbered:
        _check_duplicate(db, company.id, vt.id, fy.id, number, exclude_id=v.id)

    before = to_doc(v)
    v.voucher_type = vt
    v.financial_year_id = fy.id
    v.voucher_number = number
    _apply(v, data, r, entries, inventory)
    if v.status in (S.PENDING_APPROVAL, S.APPROVED, S.REJECTED) and source == "erp":
        v.status = S.DRAFT   # changed after review: must be re-submitted
    v.version += 1
    v.updated_by = actor.name
    _record_version(db, v, actor, source, reason, "update", before)
    if source == "erp" and _needs_sync(v):
        enqueue_outbound(db, v, actor, "upsert")
    return v


def transition(db: Session, v: Voucher, action: str, actor: Actor, *, reason: str | None = None,
               source: str = "erp", expected_version: int | None = None) -> Voucher:
    if expected_version is not None and expected_version != v.version:
        raise VersionConflict("version_conflict", f"Voucher is now version {v.version}")
    company = db.get(Company, v.company_id)
    allowed = {
        "submit": {S.DRAFT, S.REJECTED},
        "approve": {S.PENDING_APPROVAL},
        "reject": {S.PENDING_APPROVAL},
        "post": {S.DRAFT, S.APPROVED},
        "cancel": {S.DRAFT, S.PENDING_APPROVAL, S.APPROVED, S.POSTED, S.REJECTED},
        "delete": {S.DRAFT, S.PENDING_APPROVAL, S.APPROVED, S.POSTED, S.REJECTED, S.CANCELLED},
    }
    if action not in allowed:
        raise VoucherError("validation_failure", f"Unknown action {action}")
    if v.status not in allowed[action]:
        raise NotAllowed("validation_failure", f"Cannot {action} a voucher in status {v.status}")
    financial_year_for(db, company, v.voucher_date)

    before = {"status": v.status, "version": v.version}
    was_posted = v.status == S.POSTED
    if action == "submit":
        v.status = S.PENDING_APPROVAL
    elif action == "approve":
        if source == "erp" and actor.name == v.updated_by:
            raise NotAllowed("validation_failure", "Maker-checker: the person who entered the voucher cannot approve it")
        v.status = S.APPROVED
        v.approved_by = actor.name
    elif action == "reject":
        v.status = S.REJECTED
    elif action == "post":
        if v.status == S.DRAFT and _requires_approval(v, v.voucher_type):
            raise NotAllowed("validation_failure", "This voucher needs approval before posting")
        v.status = S.POSTED
        v.posted_at = utcnow()
    elif action == "cancel":
        v.status = S.CANCELLED
    elif action == "delete":
        v.status = S.DELETED
    v.version += 1
    v.updated_by = actor.name
    _record_version(db, v, actor, source, reason, action, before)

    if source == "erp":
        if action == "post":
            enqueue_outbound(db, v, actor, "upsert")
        elif action in ("cancel", "delete") and (was_posted or _was_synced(db, v)):
            enqueue_outbound(db, v, actor, action)
        elif action in ("cancel", "delete"):
            v.sync_status = SyncStatus.NOT_SYNCED
    return v


def bump_version(db: Session, v: Voucher, actor: Actor, reason: str) -> Voucher:
    """New version with unchanged content – e.g. 'keep ERP version' after a conflict."""
    v.version += 1
    v.updated_by = actor.name
    _record_version(db, v, actor, "erp", reason, "reaffirm", {"version": v.version - 1})
    return v


def get_voucher(db: Session, voucher_id: uuid.UUID) -> Voucher:
    v = db.get(Voucher, voucher_id)
    if v is None:
        raise VoucherError("not_found", "Voucher not found")
    return v
