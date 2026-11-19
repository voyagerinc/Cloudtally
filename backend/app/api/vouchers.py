import uuid
from datetime import date
from typing import Literal

from decimal import Decimal

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from sqlalchemy import func, or_, select

from app.api.deps import DB, CurrentUser, actor_of, authorize
from app.core.security import Perm, role_for
from app import audit
from app.models import Ledger, StockGroup, SyncOutbox, Voucher, VoucherStatus, VoucherType, VoucherVersion
from app.schemas.vouchers import TransitionIn, VoucherCreate, VoucherOut, VoucherSummary, VoucherUpdate
from app.services import canonical
from app.services.accounting import vouchers as engine
from app.services.sync.common import mapping_for_erp

router = APIRouter(tags=["vouchers"])

ADMIN_ROLES = ("owner", "accounts_admin")   # company admin, accounts admin (a system admin counts as owner)
ACTION_PERMS = {
    "submit": Perm.VOUCHER_WRITE, "approve": Perm.VOUCHER_APPROVE, "reject": Perm.VOUCHER_APPROVE,
    "post": Perm.VOUCHER_POST, "cancel": Perm.VOUCHER_POST, "delete": Perm.VOUCHER_DELETE,
}


def _out(db, v: Voucher) -> VoucherOut:
    mp = mapping_for_erp(db, v.company_id, "voucher", v.id)
    tally = None
    if mp:
        tally = {"guid": mp.tally_guid, "master_id": mp.tally_master_id, "alter_id": mp.tally_alter_id,
                 "last_synced_at": mp.last_synced_at, "direction": mp.sync_direction,
                 "erp_version_synced": mp.erp_version_synced}
    last = db.scalar(select(SyncOutbox).where(SyncOutbox.entity_id == v.id).order_by(SyncOutbox.created_at.desc()).limit(1))
    if last and last.last_error:
        tally = {**(tally or {}), "last_error": last.last_error, "error_code": last.error_code}
    return VoucherOut(**{k: getattr(v, k) for k in VoucherOut.model_fields if k not in ("doc", "tally", "item_groups")},
                      doc=engine.to_doc(v), tally=tally, item_groups=_item_groups(db, v))


def _item_groups(db, v: Voucher) -> dict:
    ids = {i.stock_item.group_id for i in v.inventory_lines if i.stock_item.group_id}
    names = dict(db.execute(select(StockGroup.id, StockGroup.name).where(StockGroup.id.in_(ids))).all()) if ids else {}
    return {i.stock_item.name: names.get(i.stock_item.group_id) for i in v.inventory_lines}


def _load(db, user, voucher_id: uuid.UUID, perm: str) -> Voucher:
    v = db.get(Voucher, voucher_id)
    if v is None:
        raise HTTPException(404, "Voucher not found")
    authorize(db, user, v.company_id, perm)
    return v


@router.post("/companies/{company_id}/vouchers", response_model=VoucherOut, status_code=201)
def create_voucher(company_id: uuid.UUID, body: VoucherCreate, db: DB, user: CurrentUser):
    company = authorize(db, user, company_id, Perm.VOUCHER_WRITE)
    if body.action == "post":
        authorize(db, user, company_id, Perm.VOUCHER_POST)
    data = body.model_dump(mode="json", exclude={"action"})
    v = engine.create_voucher(db, company, data, actor_of(user), action=body.action)
    db.commit()
    return _out(db, v)


@router.get("/companies/{company_id}/vouchers", response_model=list[VoucherSummary])
def list_vouchers(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                  to_date: date | None = None, voucher_type: str | None = None, status: str | None = None,
                  sync_status: str | None = None, q: str | None = None, limit: int = 100, offset: int = 0):
    authorize(db, user, company_id, Perm.VOUCHER_READ)
    stmt = (select(Voucher).join(VoucherType).outerjoin(Ledger, Ledger.id == Voucher.party_ledger_id)
            .where(Voucher.company_id == company_id))
    if from_date:
        stmt = stmt.where(Voucher.voucher_date >= from_date)
    if to_date:
        stmt = stmt.where(Voucher.voucher_date <= to_date)
    if voucher_type:
        stmt = stmt.where(VoucherType.name == voucher_type)
    stmt = stmt.where(Voucher.status == status) if status else stmt.where(Voucher.status != VoucherStatus.DELETED)
    if sync_status:
        stmt = stmt.where(Voucher.sync_status == sync_status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Voucher.voucher_number.ilike(like), Voucher.narration.ilike(like), Ledger.name.ilike(like),
                              Voucher.reference_no.ilike(like)))
    stmt = stmt.order_by(Voucher.voucher_date.desc(), Voucher.created_at.desc()).limit(min(limit, 1000)).offset(offset)
    return [VoucherSummary(id=v.id, voucher_type=v.voucher_type.name, voucher_number=v.voucher_number,
                           date=v.voucher_date, party_ledger=v.party_ledger.name if v.party_ledger else None,
                           narration=v.narration, amount=engine.voucher_total(v), status=v.status,
                           sync_status=v.sync_status, source_system=v.source_system, version=v.version)
            for v in db.scalars(stmt)]


@router.get("/companies/{company_id}/vouchers/count")
def count_vouchers(company_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.VOUCHER_READ)
    rows = db.execute(select(Voucher.status, Voucher.sync_status, func.count())
                      .where(Voucher.company_id == company_id).group_by(Voucher.status, Voucher.sync_status))
    return [{"status": s, "sync_status": ss, "count": n} for s, ss, n in rows]


@router.get("/vouchers/{voucher_id}", response_model=VoucherOut)
def get_voucher(voucher_id: uuid.UUID, db: DB, user: CurrentUser):
    return _out(db, _load(db, user, voucher_id, Perm.VOUCHER_READ))


def _is_in(line: dict) -> bool:
    q = Decimal(line["quantity"])
    return q > 0 or (q == 0 and line.get("direction") == "in")


def voucher_grid(doc: dict, groups: dict) -> list[list]:
    """A voucher as a sheet: its details, the stock coming in and the stock going out as separate tables
    (Item, Item group, Qty ...), each with a total, then the ledger entries."""
    from app.reports.xlsx import HEAD  # noqa: F401  (style names below)
    head = ["Item", "Item group", "Quantity", "Unit", "Rate", "Amount", "Godown", "Batch"]

    def band(text, width=len(head)):
        return [(text, "head")] + [("", "head")] * (width - 1)

    def num(v, style="num"):
        return (Decimal(v) if v not in (None, "") else None, style)
    grid = [[(f"{doc['voucher_type']} {doc['voucher_number']}", "bold")],
            ["Date", doc["date"]]]
    if doc.get("party_ledger"):
        grid.append(["Party", doc["party_ledger"]])
    if doc.get("narration"):
        grid.append(["Narration", doc["narration"]])
    grid.append([])
    lines = doc.get("inventory") or []
    ins, outs = [x for x in lines if _is_in(x)], [x for x in lines if not _is_in(x)]
    parts = [("In (produced / received)", ins), ("Out (consumed / issued)", outs)] if ins and outs else [("Inventory", lines)]
    for title, part in parts:
        if not part:
            continue
        grid.append(band(title))
        grid.append([(h, "head") for h in head])
        for x in part:
            grid.append([x["item"], groups.get(x["item"]), num(abs(Decimal(x["quantity"])), "num") if Decimal(x["quantity"]) else "value only",
                         x.get("unit"), num(x.get("rate")), num(x["amount"]), x.get("godown"), x.get("batch")])
        units = {x.get("unit") for x in part}
        qty = sum((abs(Decimal(x["quantity"])) for x in part), Decimal(0))
        grid.append([(f"Total {title.split(' (')[0].lower()}", "bold"), None,
                     (qty, "boldnum") if len(units) == 1 else None, (next(iter(units)), "bold") if len(units) == 1 else None, None,
                     (sum((Decimal(x["amount"]) for x in part), Decimal(0)), "boldnum")])
        grid.append([])
    if doc.get("entries"):
        grid.append(band("Ledger entries", 3))
        grid.append([(h, "head") for h in ("Ledger", "Debit", "Credit")])
        for e in doc["entries"]:
            a = Decimal(e["amount"])
            grid.append([e["ledger"], num(a) if a > 0 else None, num(-a) if a < 0 else None])
    return grid


@router.get("/vouchers/{voucher_id}/xlsx")
def voucher_xlsx(voucher_id: uuid.UUID, db: DB, user: CurrentUser):
    """The voucher as an Excel sheet; stock journals show the stock in and out as separate tables."""
    from app.api.reports import XLSX
    from app.reports import xlsx
    v = _load(db, user, voucher_id, Perm.VOUCHER_READ)
    authorize(db, user, v.company_id, Perm.EXPORT)
    doc = engine.to_doc(v)
    audit.record(db, actor_of(user), "voucher.export", "voucher", v.id, company_id=v.company_id, after={"format": "xlsx"})
    db.commit()
    name = f"{doc['voucher_type']} {doc['voucher_number']}".replace("/", "-")
    body = xlsx.build_grid(voucher_grid(doc, _item_groups(db, v)), name, widths=[42, 28, 12, 8, 14, 16, 18, 14])
    return Response(body, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})


@router.patch("/vouchers/{voucher_id}", response_model=VoucherOut)
def update_voucher(voucher_id: uuid.UUID, body: VoucherUpdate, db: DB, user: CurrentUser):
    v = _load(db, user, voucher_id, Perm.VOUCHER_WRITE)
    if v.status == VoucherStatus.POSTED:
        authorize(db, user, v.company_id, Perm.VOUCHER_POST)
    data = body.model_dump(mode="json", exclude={"expected_version", "reason"})
    engine.update_voucher(db, v, data, actor_of(user), expected_version=body.expected_version, reason=body.reason)
    db.commit()
    return _out(db, v)


@router.post("/vouchers/{voucher_id}/{action}", response_model=VoucherOut)
def voucher_action(voucher_id: uuid.UUID, action: Literal["submit", "approve", "reject", "post", "cancel"],
                   db: DB, user: CurrentUser, body: TransitionIn | None = None):
    v = _load(db, user, voucher_id, ACTION_PERMS[action])
    body = body or TransitionIn()
    engine.transition(db, v, action, actor_of(user), reason=body.reason, expected_version=body.expected_version)
    db.commit()
    return _out(db, v)


@router.delete("/vouchers/{voucher_id}", response_model=VoucherOut)
def delete_voucher(voucher_id: uuid.UUID, db: DB, user: CurrentUser, reason: str | None = None):
    """Only an admin, with remarks, and only a voucher made in the software; the delete then goes to Tally.
    A voucher that came from Tally is deleted in Tally: the next sync removes it here."""
    v = _load(db, user, voucher_id, Perm.VOUCHER_DELETE)
    if role_for(db, user, v.company_id) not in ADMIN_ROLES:
        raise HTTPException(403, "Only an admin can delete vouchers")
    if len((reason or "").strip()) < 3:
        raise HTTPException(422, "Enter remarks: why the voucher is deleted")
    if v.source_system != "erp":
        raise HTTPException(409, "This voucher came from Tally: delete it in Tally, and the next sync removes it here")
    engine.transition(db, v, "delete", actor_of(user), reason=reason.strip())
    db.commit()
    return _out(db, v)


@router.post("/vouchers/{voucher_id}/duplicate", response_model=VoucherOut, status_code=201)
def duplicate_voucher(voucher_id: uuid.UUID, db: DB, user: CurrentUser):
    src = _load(db, user, voucher_id, Perm.VOUCHER_WRITE)
    company = authorize(db, user, src.company_id, Perm.VOUCHER_WRITE)
    doc = dict(engine.to_doc(src), voucher_number=None, is_cancelled=False)
    v = engine.create_voucher(db, company, doc, actor_of(user),
                              reason=f"Duplicated from {src.voucher_type.name} {src.voucher_number}")
    db.commit()
    return _out(db, v)


@router.get("/vouchers/{voucher_id}/versions")
def voucher_versions(voucher_id: uuid.UUID, db: DB, user: CurrentUser):
    v = _load(db, user, voucher_id, Perm.VOUCHER_READ)
    rows = db.scalars(select(VoucherVersion).where(VoucherVersion.voucher_id == v.id).order_by(VoucherVersion.version)).all()
    out, prev = [], None
    for r in rows:
        changes = [] if prev is None else [{"field": d["field"], "before": d["erp"], "after": d["tally"]}
                                           for d in canonical.diff(prev.snapshot, r.snapshot)]
        if prev is not None and prev.status != r.status:
            changes.insert(0, {"field": "status", "before": prev.status, "after": r.status})
        out.append({"version": r.version, "status": r.status, "changed_by": r.changed_by, "source": r.source_system,
                    "reason": r.reason, "at": r.created_at, "hash": r.content_hash, "snapshot": r.snapshot,
                    "changes": changes})
        prev = r
    return out
