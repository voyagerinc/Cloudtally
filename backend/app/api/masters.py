import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app import audit
from app.api.deps import DB, CurrentUser, actor_of, authorize
from app.core.security import Perm
from app.models import CostCentre, Godown, Ledger, LedgerGroup, StockGroup, StockItem, Unit, VoucherType
from app.schemas.admin import GroupIn, LedgerIn, LedgerPatch, NamedIn, StockItemIn, VoucherTypeIn, VoucherTypePatch
from app.services.accounting import masters
from app.services.sync.common import mapping_for_erp

router = APIRouter(prefix="/companies/{company_id}/masters", tags=["masters"])

MASKED = ("bank_account_no", "pan")


def _mask(value: str | None) -> str | None:
    """Field-level masking of sensitive identifiers (PRD §5.5)."""
    return None if not value else "•" * max(0, len(value) - 4) + value[-4:]


def _ledger_out(db, led: Ledger, reveal: bool) -> dict:
    out = {"id": led.id, "name": led.name, "group": led.group.name, "version": led.version,
           "source_system": led.source_system, **{f: getattr(led, f) for f in masters.LEDGER_FIELDS}}
    if not reveal:
        for f in MASKED:
            out[f] = _mask(out[f])
        out["party_banks"] = [{**b, "account_no": _mask(b.get("account_no"))} for b in out["party_banks"] or []] or None
    mp = mapping_for_erp(db, led.company_id, "ledger", led.id)
    out["tally_guid"] = mp.tally_guid if mp else None
    return out


@router.get("/groups")
def list_groups(company_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.MASTERS_READ)
    groups = db.scalars(select(LedgerGroup).where(LedgerGroup.company_id == company_id).order_by(LedgerGroup.name)).all()
    return [{"id": g.id, "name": g.name, "parent": g.parent.name if g.parent else None, "nature": g.nature,
             "is_revenue": g.is_revenue, "affects_gross_profit": g.affects_gross_profit} for g in groups]


@router.post("/groups", status_code=201)
def create_group(company_id: uuid.UUID, body: GroupIn, db: DB, user: CurrentUser):
    company = authorize(db, user, company_id, Perm.MASTERS_WRITE)
    g = masters.create_group(db, company, body.model_dump(), actor_of(user))
    db.commit()
    return {"id": g.id, "name": g.name}


@router.get("/ledgers")
def list_ledgers(company_id: uuid.UUID, db: DB, user: CurrentUser, q: str | None = None, group: str | None = None,
                 limit: int = 500, offset: int = 0, reveal: bool = False):
    authorize(db, user, company_id, Perm.MASTERS_READ)
    if reveal:
        authorize(db, user, company_id, Perm.MASTERS_WRITE)
    stmt = select(Ledger).where(Ledger.company_id == company_id).order_by(Ledger.name)
    if q:
        stmt = stmt.where(Ledger.name.ilike(f"%{q}%"))
    if group:
        stmt = stmt.join(LedgerGroup).where(LedgerGroup.name == group)
    return [_ledger_out(db, led, reveal) for led in db.scalars(stmt.limit(min(limit, 5000)).offset(offset))]


@router.post("/ledgers", status_code=201)
def create_ledger(company_id: uuid.UUID, body: LedgerIn, db: DB, user: CurrentUser):
    company = authorize(db, user, company_id, Perm.MASTERS_WRITE)
    led = masters.create_ledger(db, company, body.model_dump(), actor_of(user))
    db.commit()
    return _ledger_out(db, led, True)


@router.patch("/ledgers/{ledger_id}")
def update_ledger(company_id: uuid.UUID, ledger_id: uuid.UUID, body: LedgerPatch, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.MASTERS_WRITE)
    led = db.get(Ledger, ledger_id)
    if led is None or led.company_id != company_id:
        raise HTTPException(404, "Ledger not found")
    if body.expected_version is not None and body.expected_version != led.version:
        raise HTTPException(409, f"Ledger changed meanwhile (now version {led.version})")
    masters.update_ledger(db, led, body.model_dump(exclude_unset=True), actor_of(user))
    db.commit()
    return _ledger_out(db, led, True)


@router.get("/voucher-types")
def list_voucher_types(company_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.MASTERS_READ)
    rows = db.scalars(select(VoucherType).where(VoucherType.company_id == company_id).order_by(VoucherType.name))
    return [{"id": v.id, "name": v.name, "base_type": v.base_type, "numbering": v.numbering, "prefix": v.prefix,
             "suffix": v.suffix, "next_number": v.next_number, "requires_approval": v.requires_approval,
             "approval_threshold": v.approval_threshold, "is_active": v.is_active} for v in rows]


@router.post("/voucher-types", status_code=201)
def create_voucher_type(company_id: uuid.UUID, body: VoucherTypeIn, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.MASTERS_WRITE)
    vt = VoucherType(company_id=company_id, **body.model_dump())
    db.add(vt)
    audit.record(db, actor_of(user), "voucher_type.create", "voucher_type", None, company_id=company_id, after=body.model_dump())
    db.commit()
    return {"id": vt.id, "name": vt.name}


@router.patch("/voucher-types/{vt_id}")
def update_voucher_type(company_id: uuid.UUID, vt_id: uuid.UUID, body: VoucherTypePatch, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.MASTERS_WRITE)
    vt = db.get(VoucherType, vt_id)
    if vt is None or vt.company_id != company_id:
        raise HTTPException(404, "Voucher type not found")
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(vt, k, v)
    audit.record(db, actor_of(user), "voucher_type.update", "voucher_type", vt.id, company_id=company_id, after=changes)
    db.commit()
    return {"id": vt.id, "name": vt.name, **changes}


@router.get("/stock-items")
def list_items(company_id: uuid.UUID, db: DB, user: CurrentUser, q: str | None = None):
    authorize(db, user, company_id, Perm.MASTERS_READ)
    stmt = select(StockItem).where(StockItem.company_id == company_id).order_by(StockItem.name)
    if q:
        stmt = stmt.where(StockItem.name.ilike(f"%{q}%"))
    groups = {g.id: g.name for g in db.scalars(select(StockGroup).where(StockGroup.company_id == company_id))}
    columns = [c.name for c in StockItem.__table__.columns if c.name not in ("company_id", "group_id")]
    return [{**{c: getattr(i, c) for c in columns}, "group": groups.get(i.group_id)}
            for i in db.scalars(stmt.limit(5000))]


@router.post("/stock-items", status_code=201)
def create_item(company_id: uuid.UUID, body: StockItemIn, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.MASTERS_WRITE)
    data = body.model_dump()
    group_name = data.pop("group")
    group = db.scalar(select(StockGroup).where(StockGroup.company_id == company_id, StockGroup.name == group_name)) if group_name else None
    item = StockItem(company_id=company_id, group_id=group.id if group else None, **data)
    db.add(item)
    audit.record(db, actor_of(user), "stock_item.create", "stock_item", None, company_id=company_id, after=data)
    db.commit()
    return {"id": item.id, "name": item.name}


def _simple(model, label: str, name_attr: str = "name"):
    def lister(company_id: uuid.UUID, db: DB, user: CurrentUser):
        authorize(db, user, company_id, Perm.MASTERS_READ)
        rows = db.scalars(select(model).where(model.company_id == company_id).order_by(getattr(model, name_attr)))
        columns = [c.name for c in model.__table__.columns if c.name != "company_id"]
        return [{**{c: getattr(r, c) for c in columns}, "name": getattr(r, name_attr)} for r in rows]

    def creator(company_id: uuid.UUID, body: NamedIn, db: DB, user: CurrentUser):
        authorize(db, user, company_id, Perm.MASTERS_WRITE)
        obj = model(company_id=company_id, **{name_attr: body.name})
        db.add(obj)
        audit.record(db, actor_of(user), f"{label}.create", label, None, company_id=company_id, after=body.model_dump())
        db.commit()
        return {"id": obj.id, "name": body.name}

    path = f"/{label.replace('_', '-')}s"
    router.add_api_route(path, lister, methods=["GET"], name=f"list_{label}s")
    router.add_api_route(path, creator, methods=["POST"], status_code=201, name=f"create_{label}")


_simple(Godown, "godown")
_simple(Unit, "unit", "symbol")
_simple(CostCentre, "cost_centre")
_simple(StockGroup, "stock_group")
