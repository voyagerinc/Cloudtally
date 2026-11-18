import csv
import io
import uuid
from decimal import Decimal
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select

from app import audit, reports
from app.reports import analysis, xlsx
from app.api.deps import DB, CurrentUser, actor_of, authorize
from app.core.security import Perm
from app.models import FinancialYear

router = APIRouter(prefix="/reports", tags=["reports"])

# Which list inside each report becomes the CSV rows.
CSV_ROWS = {
    "trial-balance": "lines", "day-book": "vouchers", "ledger": "lines", "stock-summary": "items",
    "outstanding": "bills", "register": "vouchers", "ledger-balances": None, "stock-item": "lines",
    "sales-analysis": "rows", "purchase-analysis": "rows", "sales-rm-cost": "rows", "fifo-costing": "items",
    "sales-fifo-cost": "rows", "sales-group-wise": "rows", "stock-journal": "rows", "material-transfer-wip": "rows",
}


def _default_period(db, company_id: uuid.UUID, from_date: date | None, to_date: date | None) -> tuple[date, date]:
    if from_date and to_date:
        return from_date, to_date
    today = date.today()
    fy = db.scalar(select(FinancialYear).where(FinancialYear.company_id == company_id,
                                               FinancialYear.start_date <= (to_date or today))
                   .order_by(FinancialYear.start_date.desc()).limit(1))
    if fy is None:
        raise HTTPException(422, "Give from/to dates; no financial year found")
    return from_date or fy.start_date, to_date or min(fy.end_date, max(today, fy.start_date))


def _respond(db, user, company_id, name: str, data, fmt: str):
    if fmt not in ("csv", "xlsx"):
        return data
    authorize(db, user, company_id, Perm.EXPORT)
    rows = data if isinstance(data, list) else data.get(CSV_ROWS.get(name) or "", [])
    audit.record(db, actor_of(user), "report.export", "report", name, company_id=company_id,
                 after={"format": fmt, "rows": len(rows)})
    db.commit()
    if fmt == "xlsx":
        sheet = list(rows)
        total = data.get("total") if isinstance(data, dict) else None
        if sheet and isinstance(total, dict):
            first = next((k for k in sheet[0] if not xlsx.HIDDEN.search(k)), "name")
            sheet.append({**{k: v for k, v in total.items() if k in sheet[0]}, first: "Total"})
        return Response(xlsx.build(sheet, name, XLSX_LABELS.get(name)), media_type=XLSX,
                        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


Fmt = Literal["json", "csv", "xlsx"]
XLSX_LABELS = {"sales-fifo-cost": {"cost": "FIFO cost"}, "sales-rm-cost": {"cost": "Raw-material cost"},
               "sales-group-wise": {"cost": "FIFO cost", "group_path": "Under"}}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/trial-balance")
def trial_balance(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                  to_date: date | None = None, by: Literal["ledger", "group"] = "ledger", format: Fmt = "json"):
    authorize(db, user, company_id, Perm.report("trial-balance"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return _respond(db, user, company_id, "trial-balance", reports.trial_balance(db, company_id, f, t, by=by), format)


@router.get("/profit-loss")
def profit_loss(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None, to_date: date | None = None):
    authorize(db, user, company_id, Perm.report("profit-loss"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return reports.profit_and_loss(db, company_id, f, t)


@router.get("/balance-sheet")
def balance_sheet(company_id: uuid.UUID, db: DB, user: CurrentUser, as_of: date | None = None):
    authorize(db, user, company_id, Perm.report("balance-sheet"))
    _, t = _default_period(db, company_id, None, as_of)
    return reports.balance_sheet(db, company_id, t)


@router.get("/day-book")
def day_book(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None, to_date: date | None = None,
             voucher_type: str | None = None, posted_only: bool = False, format: Fmt = "json"):
    authorize(db, user, company_id, Perm.report("day-book"))
    f, t = _default_period(db, company_id, from_date, to_date)
    data = reports.day_book(db, company_id, f, t, voucher_type=voucher_type, include_unposted=not posted_only)
    return _respond(db, user, company_id, "day-book", data, format)


@router.get("/ledger")
def ledger(company_id: uuid.UUID, ledger_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
           to_date: date | None = None, format: Fmt = "json"):
    authorize(db, user, company_id, Perm.report("ledger"))
    f, t = _default_period(db, company_id, from_date, to_date)
    try:
        data = reports.ledger_statement(db, company_id, ledger_id, f, t)
    except LookupError:
        raise HTTPException(404, "Ledger not found")
    return _respond(db, user, company_id, "ledger", data, format)


@router.get("/ledger-balances")
def ledger_balances(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                    to_date: date | None = None, format: Fmt = "json"):
    authorize(db, user, company_id, Perm.report("trial-balance"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return _respond(db, user, company_id, "ledger-balances", reports.ledger_balances(db, company_id, t, f), format)


@router.get("/stock-summary")
def stock_summary(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                  to_date: date | None = None, format: Fmt = "json"):
    authorize(db, user, company_id, Perm.report("stock-summary"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return _respond(db, user, company_id, "stock-summary", reports.stock_summary(db, company_id, t, f), format)


@router.get("/stock-item")
def stock_item(company_id: uuid.UUID, item_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
               to_date: date | None = None, format: Fmt = "json"):
    authorize(db, user, company_id, Perm.report("stock-summary"))
    f, t = _default_period(db, company_id, from_date, to_date)
    try:
        data = reports.stock_item_movement(db, company_id, item_id, f, t)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return _respond(db, user, company_id, "stock-item", data, format)


@router.get("/outstanding")
def outstanding(company_id: uuid.UUID, db: DB, user: CurrentUser, as_of: date | None = None,
                kind: Literal["receivable", "payable"] = "receivable", format: Fmt = "json"):
    authorize(db, user, company_id, Perm.report("receivables" if kind == "receivable" else "payables"))
    _, t = _default_period(db, company_id, None, as_of)
    return _respond(db, user, company_id, "outstanding", reports.outstanding(db, company_id, t, kind), format)


@router.get("/register")
def register(company_id: uuid.UUID, db: DB, user: CurrentUser, base_type: str = "Sales", from_date: date | None = None,
             to_date: date | None = None, format: Fmt = "json"):
    # Sales and Purchase registers can be granted one by one; the others come with "All reports".
    authorize(db, user, company_id, Perm.report(f"{base_type.lower().replace(' ', '-')}-register"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return _respond(db, user, company_id, "register", reports.register(db, company_id, base_type, f, t), format)


By = Literal["party", "date", "month", "item", "category", "stock_group", "voucher"]


def _analysis(db, user, company_id, report: str, side: str, from_date, to_date, by, party, item, category,
              stock_group, voucher_types, include_returns, format, cost: str | None):
    authorize(db, user, company_id, Perm.report(report))
    f, t = _default_period(db, company_id, from_date, to_date)
    types = [x.strip() for x in voucher_types.split(",") if x.strip()] if voucher_types else None
    data = analysis.trade_analysis(db, company_id, side, f, t, by=by, party=party, item=item, category=category,
                                   stock_group=stock_group, voucher_types=types, include_returns=include_returns,
                                   cost=cost)
    if format in ("csv", "xlsx") and by == "voucher":
        data = {**data, "rows": data["lines"]}
    return _respond(db, user, company_id, report, data, format)


@router.get("/sales-analysis")
def sales_analysis(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                   to_date: date | None = None, by: By = "party", party: uuid.UUID | None = None,
                   item: uuid.UUID | None = None, category: str | None = None, stock_group: uuid.UUID | None = None,
                   voucher_types: str | None = None, include_returns: bool = True, format: Fmt = "json"):
    """Sales by party, date, month, item, item category or stock group (credit notes count negative)."""
    return _analysis(db, user, company_id, "sales-analysis", "sales", from_date, to_date, by, party, item, category,
                     stock_group, voucher_types, include_returns, format, cost=None)


@router.get("/purchase-analysis")
def purchase_analysis(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                      to_date: date | None = None, by: By = "party", party: uuid.UUID | None = None,
                      item: uuid.UUID | None = None, category: str | None = None, stock_group: uuid.UUID | None = None,
                      voucher_types: str | None = None, include_returns: bool = True, format: Fmt = "json"):
    """Purchases by party, date, month, item, item category or stock group (debit notes count negative)."""
    return _analysis(db, user, company_id, "purchase-analysis", "purchase", from_date, to_date, by, party, item,
                     category, stock_group, voucher_types, include_returns, format, cost=None)


@router.get("/sales-rm-cost")
def sales_raw_material_cost(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                            to_date: date | None = None, by: By = "item", party: uuid.UUID | None = None,
                            item: uuid.UUID | None = None, category: str | None = None,
                            stock_group: uuid.UUID | None = None, voucher_types: str | None = None,
                            include_returns: bool = True, format: Fmt = "json"):
    """Sales with the raw-material cost of what was sold, from its production vouchers, and the margin."""
    return _analysis(db, user, company_id, "sales-rm-cost", "sales", from_date, to_date, by, party, item, category,
                     stock_group, voucher_types, include_returns, format, cost="rm")


@router.get("/sales-fifo-cost")
def sales_fifo_cost(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                    to_date: date | None = None, by: By = "voucher", party: uuid.UUID | None = None,
                    item: uuid.UUID | None = None, category: str | None = None, stock_group: uuid.UUID | None = None,
                    voucher_types: str | None = None, include_returns: bool = True, format: Fmt = "json"):
    """Sales register item-wise, with the FIFO cost of the goods sold and the margin."""
    return _analysis(db, user, company_id, "sales-fifo-cost", "sales", from_date, to_date, by, party, item, category,
                     stock_group, voucher_types, include_returns, format, cost="fifo")


@router.get("/sales-group-wise")
def sales_group_wise(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                     to_date: date | None = None, party: uuid.UUID | None = None, category: str | None = None,
                     stock_group: uuid.UUID | None = None, voucher_types: str | None = None,
                     include_returns: bool = True, format: Fmt = "json"):
    """Sales by stock group as an expandable tree: groups, their sub-groups and items, with FIFO gross profit."""
    authorize(db, user, company_id, Perm.report("sales-group-wise"))
    f, t = _default_period(db, company_id, from_date, to_date)
    types = [x.strip() for x in voucher_types.split(",") if x.strip()] if voucher_types else None
    data = analysis.group_wise(db, company_id, "sales", f, t, party=party, category=category, stock_group=stock_group,
                               voucher_types=types, include_returns=include_returns)
    return _respond(db, user, company_id, "sales-group-wise", data, format)


SJ_HEAD = ["Item", "Item group", "Qty", "Unit", "Rate", "Amount", "Godown", "Batch"]
MT_HEAD = ["Item", "Item group", "Qty", "Rate", "Amount", "Godown", "Batch"]   # quantity shown with its unit: "1 Nos"


def stock_journal_grid(data: dict, compact: bool = False) -> list[list]:
    """Each voucher as in Tally's stock journal: what came in on the left, what went out on the right.
    compact: seven columns a side with the unit inside the quantity, as the Material Transfer to WIP sheet."""
    head = MT_HEAD if compact else SJ_HEAD
    width = len(head)

    def num(v, style="num"):
        return (Decimal(v) if v not in (None, "") else None, style)

    def band(text):
        return [(text, "head")] + [("", "head")] * (width - 1)

    def qty_text(q, unit):
        return f"{Decimal(q).normalize():f} {unit or ''}".strip() if q is not None else None

    def line(x):
        if not x:
            return [None] * width
        if compact:
            return [x["item"], x["item_group"], qty_text(x["quantity"], x["unit"]), num(x["rate"]), num(x["amount"]),
                    x["godown"], x["batch"]]
        return [x["item"], x["item_group"], num(x["quantity"]), x["unit"], num(x["rate"]), num(x["amount"]), x["godown"], x["batch"]]

    def tot(t):
        if compact:
            return [("Total", "bold"), None, (qty_text(t["quantity"], t["unit"]), "bold"), None, num(t["amount"], "boldnum"), None, None]
        return [("Total", "bold"), None, num(t["quantity"], "boldnum"), (t["unit"], "bold") if t["unit"] else None, None,
                num(t["amount"], "boldnum"), None, None]
    kind = data["voucher_type"] if data["voucher_type"] != "all" else "Stock journals"
    grid = [[(f"{kind}: {data['from']} to {data['to']}", "bold")], []]
    for v in data["vouchers"]:
        grid.append([(f"{v['voucher_type']} {v['voucher_number']} · {v['date']}", "bold")])
        if v.get("narration"):
            grid.append([v["narration"]])
        n_in, n_out = len(v["in"]), len(v["out"])
        grid.append(band(f"In (produced / received) {n_in} item{'' if n_in == 1 else 's'}")
                    + band(f"Out (consumed / issued) {n_out} item{'' if n_out == 1 else 's'}"))
        grid.append([(h, "head") for h in head] * 2)
        for i in range(max(n_in, n_out)):
            grid.append(line(v["in"][i] if i < n_in else None) + line(v["out"][i] if i < n_out else None))
        grid.append(tot(v["in_total"]) + tot(v["out_total"]))
        grid.append([])
    t = data["total"]
    amount_at = head.index("Amount")
    side = [("", "head")] * width
    left, right = list(side), list(side)
    left[0] = ("Period total", "head")
    left[amount_at] = (Decimal(t["in_amount"]), "headnum")
    right[amount_at] = (Decimal(t["out_amount"]), "headnum")
    grid.append(left + right)
    grid.append([f"{t['vouchers']} vouchers"])
    return grid


@router.get("/stock-journal")
def stock_journal(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                  to_date: date | None = None, voucher_type: str | None = None, item: uuid.UUID | None = None,
                  stock_group: uuid.UUID | None = None, format: Fmt = "json"):
    """Stock journals (Material Transfer to WIP by default) with the stock in and the stock out side by side."""
    authorize(db, user, company_id, Perm.report("stock-journal"))
    f, t = _default_period(db, company_id, from_date, to_date)
    data = analysis.stock_journal_register(db, company_id, f, t, voucher_type=voucher_type, item=item,
                                           stock_group=stock_group)
    data["rows"] = [{"date": v["date"], "voucher_type": v["voucher_type"], "voucher_number": v["voucher_number"],
                     "direction": "In (produced / received)" if side == "in" else "Out (consumed / issued)",
                     **{k: x[k] for k in ("item", "item_group", "quantity", "unit", "rate", "amount", "godown", "batch")}}
                    for v in data["vouchers"] for side in ("in", "out") for x in v[side]]
    if format != "xlsx":
        return _respond(db, user, company_id, "stock-journal", data, format)
    authorize(db, user, company_id, Perm.EXPORT)
    audit.record(db, actor_of(user), "report.export", "report", "stock-journal", company_id=company_id,
                 after={"format": "xlsx", "rows": len(data["rows"])})
    db.commit()
    body = xlsx.build_grid(stock_journal_grid(data), "Stock journal", widths=[34, 22, 9, 7, 13, 15, 18, 14] * 2)
    return Response(body, media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{data["voucher_type"]} {f}.xlsx"'})


@router.get("/material-transfer-wip")
def material_transfer_wip(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                          to_date: date | None = None, item: uuid.UUID | None = None, stock_group: uuid.UUID | None = None,
                          format: Fmt = "json"):
    """Material Transfer to WIP vouchers: material in (to WIP) and out (from store) side by side."""
    authorize(db, user, company_id, Perm.report("material-transfer-wip"))
    f, t = _default_period(db, company_id, from_date, to_date)
    data = analysis.stock_journal_register(db, company_id, f, t, voucher_type=analysis.DEFAULT_JOURNAL, item=item,
                                           stock_group=stock_group)
    data["rows"] = [{"date": v["date"], "voucher_number": v["voucher_number"],
                     "direction": "In (produced / received)" if side == "in" else "Out (consumed / issued)",
                     **{k: x[k] for k in ("item", "item_group", "quantity", "unit", "rate", "amount", "godown", "batch")}}
                    for v in data["vouchers"] for side in ("in", "out") for x in v[side]]
    if format != "xlsx":
        return _respond(db, user, company_id, "material-transfer-wip", data, format)
    authorize(db, user, company_id, Perm.EXPORT)
    audit.record(db, actor_of(user), "report.export", "report", "material-transfer-wip", company_id=company_id,
                 after={"format": "xlsx", "rows": len(data["rows"])})
    db.commit()
    body = xlsx.build_grid(stock_journal_grid(data, compact=True), "Material Transfer to WIP",
                           widths=[34, 20, 11, 13, 15, 18, 14] * 2)
    return Response(body, media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="Material Transfer to WIP {f} to {t}.xlsx"'})


@router.get("/fifo-costing")
def fifo_costing(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                 to_date: date | None = None, item: uuid.UUID | None = None, category: str | None = None,
                 stock_group: uuid.UUID | None = None, format: Fmt = "json"):
    """Opening, inward, outward and closing stock per item, valued first-in-first-out."""
    authorize(db, user, company_id, Perm.report("fifo-costing"))
    f, t = _default_period(db, company_id, from_date, to_date)
    data = analysis.fifo_costing(db, company_id, f, t, item=item, category=category, stock_group=stock_group)
    return _respond(db, user, company_id, "fifo-costing", data, format)


@router.get("/filters")
def report_filters(company_id: uuid.UUID, db: DB, user: CurrentUser):
    """Choices for the analysis filters: parties, items, item categories and stock groups."""
    from sqlalchemy import func
    from app.core.security import has_perm
    from app.models import Ledger, StockGroup, StockItem, Voucher
    allowed = [k for k in ("sales-analysis", "purchase-analysis", "fifo-costing", "sales-rm-cost", "sales-fifo-cost",
                              "sales-group-wise")
               if has_perm(db, user, company_id, Perm.report(k))]
    authorize(db, user, company_id, Perm.report(allowed[0] if allowed else "sales-analysis"))
    parties = db.execute(select(Ledger.id, Ledger.name).where(
        Ledger.company_id == company_id,
        Ledger.id.in_(select(Voucher.party_ledger_id).where(Voucher.company_id == company_id).distinct()))
        .order_by(func.lower(Ledger.name))).all()
    items = db.execute(select(StockItem.id, StockItem.name).where(StockItem.company_id == company_id)
                       .order_by(func.lower(StockItem.name))).all()
    cats = db.scalars(select(StockItem.category).where(StockItem.company_id == company_id, StockItem.category.is_not(None))
                      .distinct().order_by(StockItem.category)).all()
    groups = db.execute(select(StockGroup.id, StockGroup.name).where(StockGroup.company_id == company_id)
                        .order_by(func.lower(StockGroup.name))).all()
    return {"parties": [{"id": str(i), "name": n} for i, n in parties],
            "items": [{"id": str(i), "name": n} for i, n in items],
            "categories": list(cats), "stock_groups": [{"id": str(i), "name": n} for i, n in groups]}
