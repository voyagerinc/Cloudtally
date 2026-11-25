"""Management (MIS) reports by division, their settings, and deferred expense schedules."""
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app import audit
from app.api.deps import DB, CurrentUser, actor_of, authorize
from fastapi.responses import Response

from app.api.reports import CSV_ROWS, XLSX, Fmt, _respond
from app.core.security import Perm, has_perm
from app.models import Ledger
from app.models.mis import DeferredExpense
from app.reports import mis, xlsx

router = APIRouter(tags=["mis"])

REPORT_KEYS = ("mis-pl", "mis-finance-sheet", "mis-finance-sheet-coolace", "mis-expense", "mis-exp-breakup",
               "mis-deferred")
CSV_ROWS.update({k: "rows" for k in REPORT_KEYS})


def _month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _period(from_date: date | None, to_date: date | None) -> tuple[date, date]:
    """MIS reports are monthly: with no dates, the current month; with one date, that date's month."""
    if from_date and to_date:
        if from_date > to_date:
            raise HTTPException(422, "From date is after to date")
        return from_date, to_date
    anchor = from_date or to_date or date.today()
    return anchor.replace(day=1), _month_end(anchor)


def _labels(data) -> dict:
    return {d["key"]: d["name"] for d in data["divisions"]}


# ------------------------------------------------------------------ reports

@router.get("/reports/mis-pl")
def mis_pl(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None, to_date: date | None = None,
           format: Fmt = "json"):
    """Monthly finance sheet by division: sales, RM consumption, production cost, FG cost, gross and net profit."""
    authorize(db, user, company_id, Perm.report("mis-pl"))
    f, t = _period(from_date, to_date)
    data = mis.profit_and_loss(db, company_id, f, t)
    names = _labels(data)
    data["rows"] = [{"section": s["name"], "line": ln["label"], **{names[k]: v for k, v in ln["by"].items()},
                     "total": ln["total"], **({"total_pct": ln["pct_total"]} if "pct_total" in ln else {})}
                    for s in data["sections"] for ln in s["lines"]]
    if format == "xlsx":
        return _xlsx(db, user, company_id, "mis-pl", _pl_grid(data), [30] + [18] * (len(data["divisions"]) + 1),
                     f"P&L by division {f:%b-%y}", len(data["rows"]))
    return _respond(db, user, company_id, "mis-pl", data, format)


def _xlsx(db, user, company_id, name: str, grid: list[list], widths: list[int], filename: str, rows: int):
    authorize(db, user, company_id, Perm.EXPORT)
    audit.record(db, actor_of(user), "report.export", "report", name, company_id=company_id,
                 after={"format": "xlsx", "rows": rows})
    db.commit()
    return Response(xlsx.build_grid(grid, name, widths=widths), media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{filename}.xlsx"'})


def _n(v, style="num"):
    return (Decimal(v) if v not in (None, "") else None, style)


def _pc(v, bold=False):
    return (Decimal(v) / 100 if v not in (None, "") else None, "boldpct" if bold else "pct")


def _band(text: str, width: int) -> list:
    """A group heading: bold on yellow across the table's width."""
    return [(text, "head")] + [("", "head")] * (width - 1)


def _pl_grid(data: dict) -> list[list]:
    divs = data["divisions"]
    width = len(divs) + 2
    grid = [[(f"P&L by division: {data['from']} to {data['to']}", "bold")], [],
            [(h, "head") for h in ["Particulars"] + [d["name"] for d in divs] + ["Total"]]]
    for sec in data["sections"]:
        grid.append(_band(sec["name"], width))
        for ln in sec["lines"]:
            style = "boldnum" if ln["strong"] else "num"
            grid.append([(ln["label"], "bold" if ln["strong"] else None)] + [_n(ln["by"][d["key"]], style) for d in divs]
                        + [_n(ln["total"], style)])
            if ln.get("pct"):
                grid.append(["   % of net revenue"] + [_pc(ln["pct"][d["key"]]) for d in divs] + [_pc(ln.get("pct_total"))])
        grid.append([])
    return grid


def _expense_grid(data: dict) -> list[list]:
    divs = data["divisions"]
    width = len(divs) + 2
    grid = [[(f"Expenses by division: {data['from']} to {data['to']}", "bold")], [],
            [(h, "head") for h in ["Head"] + [d["name"] for d in divs] + ["Total"]]]
    for sec in data["sections"]:
        grid.append(_band(sec["name"], width))
        for r in sec["rows"]:
            grid.append([r["head"]] + [_n(r["by"][d["key"]]) for d in divs] + [_n(r["total"])])
        grid.append([(f"Total {sec['name'].lower()}", "bold")] + [_n(sec["by"][d["key"]], "boldnum") for d in divs]
                    + [_n(sec["total"], "boldnum")])
        grid.append([])
    totals = [sum((Decimal(sec["by"][d["key"]]) for sec in data["sections"]), Decimal(0)) for d in divs]
    grid.append([("Total overheads", "head")] + [(v, "headnum") for v in totals] + [(sum(totals, Decimal(0)), "headnum")])
    return grid


def _breakup_grid(data: dict) -> list[list]:
    divs = data["divisions"]
    head = ["Ledger", "Head", "In Tally", "Deferred", "Amount", "%"] + [d["name"] for d in divs]
    grid = [[(f"Expense breakup: {data['from']} to {data['to']}", "bold")], [], [(h, "head") for h in head]]
    for sec in data["sections"]:
        grid.append(_band(sec["name"], len(head)))
        for r in sec["rows"]:
            grid.append([r["ledger"], r["head"], _n(r["tally"]), _n(r["deferred"] if Decimal(r["deferred"]) else None),
                         _n(r["amount"]), _pc(r["pct"])] + [_n(r["by"][d["key"]]) for d in divs])
        grid.append([(f"Total {sec['name'].lower()}", "bold"), None, None, None, _n(sec["total"], "boldnum"), None]
                    + [_n(sec["by"][d["key"]], "boldnum") for d in divs])
        grid.append([])
    return grid


def _sheet_grid(data: dict) -> list[list]:
    """The finance sheet as the workbook lays it out: blocks one under another, Month | Category | figures | %."""
    def num(v, bold=False):
        if isinstance(v, str) and v and not re.fullmatch(r"-?\d+(\.\d+)?", v):
            return (v, "bold" if bold else None)          # a name (the transfer block's division and stock)
        return (Decimal(str(v)) if v not in (None, "") else None, "boldnum" if bold else "num")
    grid = [[(f"Monthly Finance Sheet - {data['period']}", "bold")], []]

    def block(b, with_month=True):
        head = (["Month"] if with_month else []) + ["Category"] + b["columns"] + (["%"] if b.get("pct") else [])
        grid.append(_band(b["title"], len(head)))
        grid.append([(h, "head") for h in head])
        for r in b["rows"]:
            bold = bool(r.get("total"))
            grid.append(([r.get("month") or None] if with_month else []) + [(r["category"], "bold" if bold else None)]
                        + [num(v, bold) for v in r["values"]] + ([_pc(r.get("pct"), bold)] if b.get("pct") else []))
        grid.append([])
    for i, b in enumerate(data["blocks"]):
        block(b)
        if i == 0 and data.get("transfers") is not None:
            block(_transfer_block(data), with_month=False)
        if i == 0 and data["purchase"]["columns"] != ["Total"]:
            block(data["purchase"], with_month=False)
    return grid


@router.get("/reports/mis-finance-sheet")
def mis_finance_sheet(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                      to_date: date | None = None, format: Fmt = "json"):
    """The P&L in the finance workbook's layout: RM store, production cost (WIP), finished goods cost, sales,
    P&L, net profit and GP on RM, each with a row per division and a total."""
    authorize(db, user, company_id, Perm.report("mis-finance-sheet"))
    f, t = _period(from_date, to_date)
    data = mis.finance_sheet(db, company_id, f, t)
    data["rows"] = [{"block": b["title"], "month": r.get("month"), "category": r["category"],
                     **dict(zip(b["columns"], r["values"])), **({"%": r.get("pct")} if b.get("pct") else {})}
                    for b in data["blocks"] + [data["purchase"]] for r in b["rows"]]
    if format != "xlsx":
        return _respond(db, user, company_id, "mis-finance-sheet", data, format)
    return _xlsx(db, user, company_id, "mis-finance-sheet", _sheet_grid(data), [11, 20, 18, 18, 18, 18, 18, 10],
                 f"Monthly P&L {data['period']}", len(data["rows"]))


KIND_NAMES = {"rm": "Raw material", "wip": "WIP", "fg": "Finished goods"}


def _transfer_block(data: dict) -> dict:
    """Material taken from one division's stock for another division's production, as a sheet block."""
    rows = data.get("transfers") or []
    return {"title": "Material used by another division", "columns": ["Taken From", "Stock", "Vouchers", "Value"],
            "rows": [{"category": r["to"], "values": [r["from"], KIND_NAMES.get(r["kind"], r["kind"]), r["vouchers"], r["value"]]}
                     for r in rows]
            + [{"category": "Total", "total": True,
                "values": ["", "", sum(r["vouchers"] for r in rows), str(sum((Decimal(r["value"]) for r in rows), Decimal(0)))]}]}


@router.get("/reports/mis-finance-sheet-coolace")
def mis_finance_sheet_coolace(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                              to_date: date | None = None, format: Fmt = "json"):
    """The monthly finance sheet with material one division's production takes from another division charged
    to the division that produced the item (a customised cooler built from a cooler and hydraulic parts: their
    value is in Customised Cooler, and less in Cooler and Hydraulic)."""
    key = "mis-finance-sheet-coolace"
    authorize(db, user, company_id, Perm.report(key))
    f, t = _period(from_date, to_date)
    data = mis.finance_sheet(db, company_id, f, t, transfers=True)
    data["rows"] = [{"block": b["title"], "month": r.get("month"), "category": r["category"],
                     **dict(zip(b["columns"], r["values"])), **({"%": r.get("pct")} if b.get("pct") else {})}
                    for b in data["blocks"] + [data["purchase"]] for r in b["rows"]]
    data["rows"] += [{"block": "Material used by another division", "category": r["to"], "taken_from": r["from"],
                      "stock": KIND_NAMES.get(r["kind"], r["kind"]), "vouchers": r["vouchers"], "value": r["value"]}
                     for r in data["transfers"] or []]
    if format != "xlsx":
        return _respond(db, user, company_id, key, data, format)
    return _xlsx(db, user, company_id, key, _sheet_grid(data), [11, 20] + [18] * 8 + [10],
                 f"Monthly P&L {data['period']}", len(data["rows"]))


@router.get("/reports/mis-transfers")
def mis_transfers(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                  to_date: date | None = None, src: str | None = None, dst: str | None = None,
                  kind: str | None = None, format: Fmt = "json"):
    """The items behind a figure of the Coolace sheet: what a division's production took from another
    division's stock, voucher by voucher (filter by the division it came from, went to, and the stock)."""
    authorize(db, user, company_id, Perm.report("mis-finance-sheet-coolace"))
    f, t = _period(from_date, to_date)
    settings = mis.load_settings(db, company_id)
    data = mis.inter_division(db, company_id, f, t, settings, detail=True)
    lines = [ln for ln in data["lines"] if (not src or ln["from_key"] == src) and (not dst or ln["to_key"] == dst)
             and (not kind or ln["kind"] == kind)]
    rows = [{"date": ln["date"], "voucher_type": ln["voucher_type"], "voucher_number": ln["voucher_number"],
             "taken_from": ln["from"], "used_by": ln["to"], "stock": KIND_NAMES.get(ln["kind"], ln["kind"]),
             "item": ln["item"], "quantity": ln["quantity"], "unit": ln["unit"], "value": ln["value"],
             "produced": ", ".join(ln["produced"])} for ln in lines]
    out = {"from": str(f), "to": str(t), "divisions": settings["divisions"], "lines": lines, "rows": rows,
           "total": str(sum((Decimal(ln["value"]) for ln in lines), Decimal(0))),
           "vouchers": len({ln["voucher_id"] for ln in lines})}
    return _respond(db, user, company_id, "mis-finance-sheet-coolace", out, format)


@router.get("/reports/mis-expense")
def mis_expense(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                to_date: date | None = None, format: Fmt = "json"):
    """Factory and admin overheads by expense head and division."""
    authorize(db, user, company_id, Perm.report("mis-expense"))
    f, t = _period(from_date, to_date)
    data = mis.expenses(db, company_id, f, t)
    names = _labels(data)
    data["rows"] = [{"overhead": s["name"], "head": r["head"], **{names[k]: v for k, v in r["by"].items()}, "total": r["total"]}
                    for s in data["sections"] for r in s["rows"]]
    if format == "xlsx":
        return _xlsx(db, user, company_id, "mis-expense", _expense_grid(data), [34] + [18] * (len(data["divisions"]) + 1),
                     f"Expenses {f:%b-%y}", len(data["rows"]))
    return _respond(db, user, company_id, "mis-expense", data, format)


@router.get("/reports/mis-exp-breakup")
def mis_exp_breakup(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                    to_date: date | None = None, format: Fmt = "json"):
    """Each overhead ledger with its share of the total and its split by division."""
    authorize(db, user, company_id, Perm.report("mis-exp-breakup"))
    f, t = _period(from_date, to_date)
    data = mis.expense_breakup(db, company_id, f, t)
    names = _labels(data)
    data["rows"] = [{"overhead": s["name"], "ledger": r["ledger"], "head": r["head"], "in_tally": r["tally"],
                     "deferred": r["deferred"], "amount": r["amount"], "pct": r["pct"],
                     **{names[k]: v for k, v in r["by"].items()}} for s in data["sections"] for r in s["rows"]]
    if format == "xlsx":
        return _xlsx(db, user, company_id, "mis-exp-breakup", _breakup_grid(data),
                     [36, 26, 16, 14, 16, 9] + [16] * len(data["divisions"]), f"Expense breakup {f:%b-%y}", len(data["rows"]))
    return _respond(db, user, company_id, "mis-exp-breakup", data, format)


@router.get("/reports/mis-deferred")
def mis_deferred(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                 to_date: date | None = None, format: Fmt = "json"):
    """Deferred expense schedules: the P&L charge and the balance still to charge, month by month.
    With no dates, the financial year (April to March) of today."""
    authorize(db, user, company_id, Perm.report("mis-deferred"))
    if not (from_date and to_date):
        anchor = from_date or to_date or date.today()
        start = date(anchor.year if anchor.month >= 4 else anchor.year - 1, 4, 1)
        from_date, to_date = from_date or start, to_date or date(start.year + 1, 3, 31)
    data = mis.deferred_report(db, company_id, from_date, to_date)
    data["rows"] = [{"head": s["head"], "description": s["description"], "total_amount": s["total_amount"],
                     "first_month": s["first_month"], "months": s["months"], "monthly": s["monthly"],
                     **{f"charge {mo}": c for mo, c in zip(data["months"], s["charge"])},
                     **{f"balance {mo}": b for mo, b in zip(data["months"], s["balance"])}} for s in data["schedules"]]
    return _respond(db, user, company_id, "mis-deferred", data, format)


# ------------------------------------------------------------------ settings

def _can_read(db, user, company_id):
    if not any(has_perm(db, user, company_id, Perm.report(k)) for k in REPORT_KEYS):
        authorize(db, user, company_id, Perm.report("mis-pl"))


@router.get("/mis/settings")
def get_settings(company_id: uuid.UUID, db: DB, user: CurrentUser):
    """Divisions and how cost centres, stock groups and P&L ledgers map to them (with the guessed defaults)."""
    _can_read(db, user, company_id)
    s = mis.load_settings(db, company_id)
    groups = mis._company_groups(db, company_id)
    sg = mis.stock_group_map(s)
    stock_groups = []
    for gid, g in sorted(s["_groups"].items(), key=lambda x: x[1].name.lower()):
        own = s["stock_groups_explicit"].get(gid, {})
        div, kind = sg.get(gid, (None, None))
        parent = s["_groups"].get(g.parent_id)
        stock_groups.append({"id": str(gid), "name": g.name, "parent": parent.name if parent else None,
                             "division": own.get("division"), "kind": own.get("kind"), "saved": own.get("saved", False),
                             "effective_division": div, "effective_kind": kind})
    ledgers = []
    for led in db.scalars(select(Ledger).where(Ledger.company_id == company_id).order_by(Ledger.name)):
        g = groups.get(led.group_id)
        primary = g["primary"].name if g else None
        if primary not in mis.CLASS_OF_PRIMARY and str(led.id) not in s["ledgers"]:
            continue
        own = mis.ledger_setting(s, led.id)
        ledgers.append({"id": str(led.id), "name": led.name, "group": g["group"].name if g else None, "primary": primary,
                        "class": mis.ledger_class(led, groups, s), "default_class": mis.CLASS_OF_PRIMARY.get(primary),
                        "head": mis.ledger_head(led, groups, s), "default_head": g["group"].name if g else led.name,
                        "division": own.get("division"), "split": own.get("split"), "deferred": own.get("deferred") or "add"})
    return {"divisions": s["divisions"], "classes": mis.CLASSES, "kinds": {"rm": "Raw material", "wip": "WIP", "fg": "Finished goods"},
            "cost_centres": [{"name": k, "division": v} for k, v in s["cost_centres"].items()],
            "stock_groups": stock_groups, "ledgers": ledgers, "stock_overrides": s["stock_overrides"],
            "can_edit": has_perm(db, user, company_id, Perm.MASTERS_WRITE)}


class Division(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]{1,20}$")
    name: str = Field(min_length=1, max_length=60)


class SettingsIn(BaseModel):
    divisions: list[Division] | None = None
    cost_centres: dict[str, str] | None = None
    stock_groups: dict[str, dict | None] | None = None
    ledgers: dict[str, dict | None] | None = None
    stock_overrides: dict[str, dict | None] | None = None


@router.put("/mis/settings")
def put_settings(company_id: uuid.UUID, body: SettingsIn, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.MASTERS_WRITE)
    changes = body.model_dump(exclude_none=True)
    if "divisions" in changes and len(changes["divisions"]) < 1:
        raise HTTPException(422, "Keep at least one division")
    keys = {d["key"] for d in (changes.get("divisions") or mis.load_settings(db, company_id)["divisions"])}
    for name, div in (changes.get("cost_centres") or {}).items():
        if div not in keys | {mis.SHARED}:
            raise HTTPException(422, f"Cost centre {name}: unknown division {div}")
    for lid, s in (changes.get("ledgers") or {}).items():
        if s is None:
            continue
        if s.get("class") and s["class"] not in mis.CLASSES:
            raise HTTPException(422, f"Unknown class {s['class']}")
        if s.get("division") and s["division"] not in keys | {mis.SHARED}:
            raise HTTPException(422, f"Unknown division {s['division']}")
        split = s.get("split") or {}
        if any(k not in keys or Decimal(str(v or 0)) < 0 for k, v in split.items()):
            raise HTTPException(422, "Split percentages must be for known divisions and not negative")
    cfg = mis.save_settings(db, company_id, changes, actor_of(user).name)
    audit.record(db, actor_of(user), "mis.settings.update", "company", company_id, company_id=company_id, after=changes)
    db.commit()
    return {"ok": True, "saved": list(changes), "divisions": cfg.get("divisions") or mis.DEFAULT_DIVISIONS}


# ------------------------------------------------------------------ deferred expense schedules

class DeferredIn(BaseModel):
    head: str = Field(min_length=1, max_length=200)
    ledger_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=1000)
    total_amount: Decimal = Field(gt=0, max_digits=16, decimal_places=2)
    first_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    months: int = Field(ge=1, le=120)

    @field_validator("head")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


def _deferred_out(s: DeferredExpense) -> dict:
    return {"id": str(s.id), "head": s.head, "ledger_id": str(s.ledger_id) if s.ledger_id else None,
            "description": s.description, "total_amount": str(s.total_amount), "first_month": s.first_month.strftime("%Y-%m"),
            "months": s.months}


def _apply(db, company_id, s: DeferredExpense, body: DeferredIn):
    if body.ledger_id:
        led = db.get(Ledger, body.ledger_id)
        if led is None or led.company_id != company_id:
            raise HTTPException(422, "Ledger not found in this company")
    y, mo = map(int, body.first_month.split("-"))
    s.head, s.ledger_id, s.description = body.head, body.ledger_id, body.description
    s.total_amount, s.first_month, s.months = body.total_amount, date(y, mo, 1), body.months


@router.get("/mis/deferred")
def list_deferred(company_id: uuid.UUID, db: DB, user: CurrentUser):
    _can_read(db, user, company_id)
    rows = db.scalars(select(DeferredExpense).where(DeferredExpense.company_id == company_id)
                      .order_by(DeferredExpense.head, DeferredExpense.first_month))
    return [_deferred_out(s) for s in rows]


@router.post("/mis/deferred", status_code=201)
def add_deferred(company_id: uuid.UUID, body: DeferredIn, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.MASTERS_WRITE)
    s = DeferredExpense(company_id=company_id, created_by=actor_of(user).name)
    _apply(db, company_id, s, body)
    db.add(s)
    db.flush()
    audit.record(db, actor_of(user), "mis.deferred.create", "deferred_expense", s.id, company_id=company_id, after=_deferred_out(s))
    db.commit()
    return _deferred_out(s)


def _own(db, company_id, deferred_id) -> DeferredExpense:
    s = db.get(DeferredExpense, deferred_id)
    if s is None or s.company_id != company_id:
        raise HTTPException(404, "Schedule not found")
    return s


@router.put("/mis/deferred/{deferred_id}")
def update_deferred(company_id: uuid.UUID, deferred_id: uuid.UUID, body: DeferredIn, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.MASTERS_WRITE)
    s = _own(db, company_id, deferred_id)
    before = _deferred_out(s)
    _apply(db, company_id, s, body)
    audit.record(db, actor_of(user), "mis.deferred.update", "deferred_expense", s.id, company_id=company_id,
                 before=before, after=_deferred_out(s))
    db.commit()
    return _deferred_out(s)


@router.delete("/mis/deferred/{deferred_id}", status_code=204)
def delete_deferred(company_id: uuid.UUID, deferred_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.MASTERS_WRITE)
    s = _own(db, company_id, deferred_id)
    audit.record(db, actor_of(user), "mis.deferred.delete", "deferred_expense", s.id, company_id=company_id, before=_deferred_out(s))
    db.delete(s)
    db.commit()
