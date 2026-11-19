import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

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
    counts = dict(db.execute(select(Ledger.group_id, func.count()).where(Ledger.company_id == company_id)
                             .group_by(Ledger.group_id)).all())
    return [{"id": g.id, "name": g.name, "parent": g.parent.name if g.parent else None, "nature": g.nature,
             "is_revenue": g.is_revenue, "affects_gross_profit": g.affects_gross_profit,
             "is_reserved": g.is_reserved, "ledgers": counts.get(g.id, 0)} for g in groups]


NATURES = {"asset": "Assets", "liability": "Liabilities", "income": "Income", "expense": "Expenses"}


@router.get("/ledger-categories")
def ledger_categories(company_id: uuid.UUID, db: DB, user: CurrentUser):
    """Tally's four kinds of ledger (assets, liabilities, income, expenses), with their primary groups."""
    authorize(db, user, company_id, Perm.MASTERS_READ)
    groups = db.scalars(select(LedgerGroup).where(LedgerGroup.company_id == company_id)).all()
    by_id = {g.id: g for g in groups}
    counts = dict(db.execute(select(Ledger.group_id, func.count()).where(Ledger.company_id == company_id)
                             .group_by(Ledger.group_id)).all())

    def primary(g):
        seen = set()
        while g.parent_id in by_id and g.id not in seen:
            seen.add(g.id)
            g = by_id[g.parent_id]
        return g
    out = {k: {"key": k, "name": v, "primary_groups": set(), "groups": 0, "ledgers": 0} for k, v in NATURES.items()}
    for g in groups:
        row = out.setdefault(g.nature, {"key": g.nature, "name": g.nature, "primary_groups": set(), "groups": 0, "ledgers": 0})
        row["primary_groups"].add(primary(g).name)
        row["groups"] += 1
        row["ledgers"] += counts.get(g.id, 0)
    return [{**r, "primary_groups": sorted(r["primary_groups"])} for r in out.values()]


@router.get("/stock-groups")
def list_stock_groups(company_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.MASTERS_READ)
    groups = db.scalars(select(StockGroup).where(StockGroup.company_id == company_id).order_by(StockGroup.name)).all()
    names = {g.id: g.name for g in groups}
    counts = dict(db.execute(select(StockItem.group_id, func.count()).where(StockItem.company_id == company_id)
                             .group_by(StockItem.group_id)).all())
    return [{"id": g.id, "name": g.name, "parent": names.get(g.parent_id), "hsn_code": g.hsn_code,
             "gst_rate": g.gst_rate, "taxability": g.taxability, "items": counts.get(g.id, 0)} for g in groups]


@router.get("/stock-categories")
def list_stock_categories(company_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.MASTERS_READ)
    rows = db.execute(select(StockItem.category, func.count()).where(StockItem.company_id == company_id,
                                                                     StockItem.category.is_not(None))
                      .group_by(StockItem.category).order_by(StockItem.category)).all()
    return [{"name": name, "items": n} for name, n in rows]


@router.get("/units")
def list_units(company_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.MASTERS_READ)
    units = db.scalars(select(Unit).where(Unit.company_id == company_id).order_by(Unit.symbol)).all()
    counts = dict(db.execute(select(StockItem.unit, func.count()).where(StockItem.company_id == company_id)
                             .group_by(StockItem.unit)).all())
    return [{"id": u.id, "symbol": u.symbol, "formal_name": u.formal_name, "decimals": u.decimals,
             "base_unit": u.base_unit, "additional_unit": u.additional_unit, "conversion": u.conversion,
             "items": counts.get(u.symbol, 0)} for u in units]


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
             "approval_threshold": v.approval_threshold, "is_active": v.is_active,
             "affects_accounts": v.affects_accounts, "affects_stock": v.affects_stock} for v in rows]


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


# ------------------------------------------------------------------ BOM (bill of materials)

def _bom_rows(db, company_id: uuid.UUID, q: str | None = None) -> list[dict]:
    """Every item with a BOM, its components with their item group, primary group (the group straight
    under Primary) and the unit of the component."""
    from app.reports.analysis import _main_groups
    groups = dict(db.execute(select(StockGroup.id, StockGroup.name).where(StockGroup.company_id == company_id)).all())
    mains = _main_groups(db, company_id)
    items = db.scalars(select(StockItem).where(StockItem.company_id == company_id)).all()
    info = {i.name: (groups.get(i.group_id), i.unit, str(i.id), mains.get(i.group_id)) for i in items}
    q = (q or "").strip().lower()
    out = []
    for i in sorted((i for i in items if i.bom), key=lambda i: i.name.lower()):
        boms = []
        for b in i.bom:
            comps = []
            for c in b["components"]:
                group, _unit, item_id, main = info.get(c["item"], (None, None, None, None))
                comps.append({**c, "item_group": group, "primary_group": main, "item_id": item_id})
            boms.append({**b, "components": comps})
        if q and not (q in i.name.lower() or q in (groups.get(i.group_id) or "").lower()
                      or any(q in c["item"].lower() for b in boms for c in b["components"])):
            continue
        out.append({"id": str(i.id), "item": i.name, "item_group": groups.get(i.group_id),
                    "primary_group": mains.get(i.group_id), "unit": i.unit, "boms": boms})
    return out


@router.get("/bom")
def list_bom(company_id: uuid.UUID, db: DB, user: CurrentUser, q: str | None = None):
    authorize(db, user, company_id, Perm.MASTERS_READ)
    return _bom_rows(db, company_id, q)


@router.get("/bom/xlsx")
def bom_xlsx(company_id: uuid.UUID, db: DB, user: CurrentUser, q: str | None = None):
    """All BOMs in one sheet: each item with its components under it, headings bold on yellow."""
    from fastapi.responses import Response
    from app.reports import xlsx
    from app.api.reports import XLSX
    authorize(db, user, company_id, Perm.MASTERS_READ)
    authorize(db, user, company_id, Perm.EXPORT)
    rows = _bom_rows(db, company_id, q)
    head = ["Item", "Item group", "Primary group", "BOM", "For qty", "Component", "Component group",
            "Component primary group", "Nature", "Qty", "Unit", "Godown"]
    grid = [[("Bill of materials (BOM)", "bold")], [f"{len(rows)} items"], [], [(h, "head") for h in head]]
    for r in rows:
        for b in r["boms"]:
            first = True
            for c in b["components"]:
                grid.append([(r["item"], "bold") if first else None, r["item_group"] if first else None,
                             r["primary_group"] if first else None, b["name"] if first else None,
                             (f"{b['basic_qty']} {b['unit'] or ''}".strip(), "bold") if first else None,
                             c["item"], c["item_group"], c["primary_group"], c["nature"], (c["qty"], "num"), c["unit"],
                             c["godown"]])
                first = False
            grid.append([])
    audit.record(db, actor_of(user), "report.export", "report", "bom", company_id=company_id,
                 after={"format": "xlsx", "rows": len(rows)})
    db.commit()
    body = xlsx.build_grid(grid, "BOM", widths=[34, 22, 22, 18, 10, 40, 24, 24, 12, 10, 8, 16])
    return Response(body, media_type=XLSX, headers={"Content-Disposition": 'attachment; filename="Bill of materials.xlsx"'})


@router.post("/bom/refresh")
def refresh_bom(company_id: uuid.UUID, db: DB, user: CurrentUser):
    """Read every item's BOM from Tally now (read only). Later master syncs keep them up to date."""
    from app.connectors.tally import TallyError
    from app.models import TallyConnector
    from app.services.sync.common import client_for
    authorize(db, user, company_id, Perm.SYNC_READ)
    conn = db.get(TallyConnector, company_id)
    if conn is None or not conn.enabled:
        raise HTTPException(409, "This company is not connected to Tally")
    try:
        boms = client_for(conn).stock_item_boms()
    except TallyError as exc:
        raise HTTPException(502, f"Tally: {exc.message}")
    items = db.scalars(select(StockItem).where(StockItem.company_id == company_id)).all()
    by_name = {i.name: i for i in items}
    updated = 0
    for i in items:
        new = boms.get(i.name)
        if (i.bom or None) != new:
            i.bom = new
            updated += 1
    missing = sorted(n for n in boms if n not in by_name)
    audit.record(db, actor_of(user), "masters.bom.refresh", "company", company_id, company_id=company_id,
                 after={"items_with_bom": len(boms), "updated": updated, "not_in_software": len(missing)})
    db.commit()
    return {"items_with_bom": len(boms), "updated": updated, "not_in_software": missing[:50]}


@router.get("/fifo-rates")
def fifo_rates(company_id: uuid.UUID, db: DB, user: CurrentUser, as_of: date, item: list[str] = Query(default=[]),
               exclude_voucher: uuid.UUID | None = None):
    """FIFO closing rate of the given items on a date: the default rate of a stock line typed for that day.
    exclude_voucher leaves out the voucher being edited (its own lines would change the rate)."""
    from app.reports.analysis import fifo_rates as rates
    authorize(db, user, company_id, Perm.MASTERS_READ)
    return rates(db, company_id, as_of, item[:500], exclude_voucher)
