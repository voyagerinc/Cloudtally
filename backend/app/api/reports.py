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
from app.connectors.tally import TallyError
from app.core.security import Perm, has_perm
from app.models import FinancialYear, TallyConnector

router = APIRouter(prefix="/reports", tags=["reports"])

# Which list inside each report becomes the CSV rows.
CSV_ROWS = {
    "trial-balance": "lines", "day-book": "vouchers", "ledger": "lines", "stock-summary": "items",
    "outstanding": "bills", "register": "vouchers", "ledger-balances": None, "stock-item": "lines",
    "sales-analysis": "rows", "purchase-analysis": "rows", "sales-rm-cost": "rows", "fifo-costing": "items", "fifo-monthly": "rows", "godown-summary": "rows",
    "sales-fifo-cost": "rows", "sales-group-wise": "rows", "sales-group-units": "rows", "stock-journal": "rows", "material-transfer-wip": "rows",
    "material-transfer-wip-main": "rows", "wo-manufacturing": "rows",
    "gstr1": "rows", "gstr2": "rows", "hsn-summary": "rows", "einvoice": "rows", "eway-bill": "rows", "outstanding-monthly": "rows", "custom-fields": "rows", "cash-flow": "rows", "funds-flow": "rows",
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
               "sales-group-wise": {"cost": "FIFO cost", "group_path": "Under"},
               "sales-group-units": {"group_path": "Under", "quantity": "Qty (main unit)", "unit": "Main unit",
                                     "rate": "Sale rate (main unit)", "alt_quantity": "Qty (alternate unit)",
                                     "alt_unit": "Alternate unit", "alt_rate": "Sale rate (alternate unit)",
                                     "value": "Sales value", "cost_rate": "FIFO cost rate (main unit)",
                                     "cost_alt_rate": "FIFO cost rate (alternate unit)", "cost": "FIFO cost",
                                     "margin": "Gross profit", "margin_pct": "Margin %"}}
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


@router.get("/cash-flow")
def cash_flow(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
              to_date: date | None = None, format: Fmt = "json"):
    """Money in and out of the cash and bank accounts, by the group of the other side, and month by month."""
    from app.reports import flows
    authorize(db, user, company_id, Perm.report("cash-flow"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return _respond(db, user, company_id, "cash-flow", flows.cash_flow(db, company_id, f, t), format)


@router.get("/funds-flow")
def funds_flow(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
               to_date: date | None = None, format: Fmt = "json"):
    """Sources and applications of funds, and the change in working capital."""
    from app.reports import flows
    authorize(db, user, company_id, Perm.report("funds-flow"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return _respond(db, user, company_id, "funds-flow", flows.funds_flow(db, company_id, f, t), format)


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
                kind: Literal["receivable", "payable"] = "receivable", ageing: str = "30,60,90,120,150,180,365",
                age_by: Literal["bill", "due"] = "bill", fifo: bool = True,
                basis: Literal["voucher", "eway"] = "voucher", format: Fmt = "json"):
    """ageing: the periods' upper limits in days, e.g. "15,30,60,90,180" (the user chooses them on the report).
    fifo: set On Account payments / receipts / journals off against the oldest bills."""
    own = Perm.report("receivables" if kind == "receivable" else "payables")
    # Ageing by e-way bill date: an option of the receivables / payables report, and a report of its own.
    if basis == "eway" and kind == "receivable" and not has_perm(db, user, company_id, own):
        own = Perm.report("receivables-eway")
    authorize(db, user, company_id, own)
    try:
        limits = sorted({int(x) for x in ageing.replace(" ", "").split(",") if x})
    except ValueError:
        raise HTTPException(422, "Ageing periods are whole numbers of days, e.g. 30,60,90")
    if not limits or len(limits) > 12 or limits[0] < 1 or limits[-1] > 3650:
        raise HTTPException(422, "Give 1 to 12 ageing periods between 1 and 3650 days, e.g. 30,60,90")
    _, t = _default_period(db, company_id, None, as_of)
    return _respond(db, user, company_id, "outstanding",
                    reports.outstanding(db, company_id, t, kind, ageing=limits, age_by=age_by, fifo=fifo,
                                        basis=basis), format)


@router.get("/outstanding-monthly")
def outstanding_monthly(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                        to_date: date | None = None, fifo: bool = True, format: Fmt = "json"):
    """Receivables at the end of the period by the month their due starts (the e-way bill date, else the
    bill date): Opening, a column per month of the period, and Closing."""
    authorize(db, user, company_id, Perm.report("receivables-eway-monthly"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return _respond(db, user, company_id, "outstanding-monthly",
                    reports.outstanding_monthly(db, company_id, f, t, "receivable", fifo=fifo), format)


# ------------------------------------------------------------------ GST

@router.get("/gstr1")
def gstr1(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None, to_date: date | None = None,
          format: Fmt = "json"):
    """Outward supplies by GSTR-1 section (B2B, B2CL, B2CS, exports, credit/debit notes), one row per invoice and rate."""
    from app.reports import gst
    authorize(db, user, company_id, Perm.report("gstr1"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return _respond(db, user, company_id, "gstr1", gst.gstr(db, company_id, f, t, "outward"), format)


@router.get("/gstr2")
def gstr2(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None, to_date: date | None = None,
          format: Fmt = "json"):
    """Inward supplies (purchases, debit notes) by section with input tax, one row per invoice and rate."""
    from app.reports import gst
    authorize(db, user, company_id, Perm.report("gstr2"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return _respond(db, user, company_id, "gstr2", gst.gstr(db, company_id, f, t, "inward"), format)


@router.get("/hsn-summary")
def hsn_summary(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                to_date: date | None = None, side: Literal["outward", "inward"] = "outward", format: Fmt = "json"):
    from app.reports import gst
    authorize(db, user, company_id, Perm.report("hsn-summary"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return _respond(db, user, company_id, "hsn-summary", gst.hsn_summary(db, company_id, f, t, side), format)


@router.get("/einvoice")
def einvoice(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
             to_date: date | None = None, format: Fmt = "json"):
    from app.reports import gst
    authorize(db, user, company_id, Perm.report("einvoice"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return _respond(db, user, company_id, "einvoice", gst.einvoice(db, company_id, f, t), format)


@router.get("/eway-bill")
def eway_bill(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
              to_date: date | None = None, format: Fmt = "json"):
    from app.reports import gst
    authorize(db, user, company_id, Perm.report("eway-bill"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return _respond(db, user, company_id, "eway-bill", gst.eway_bill(db, company_id, f, t), format)


@router.get("/custom-fields")
def custom_fields_report(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                         to_date: date | None = None, voucher_type: str | None = None, format: Fmt = "json"):
    """Fields a Tally customisation keeps (e.g. chassis no., motor no.), per item line / batch or voucher."""
    from app.services.sync import custom_fields
    authorize(db, user, company_id, Perm.report("custom-fields"))
    f, t = _default_period(db, company_id, from_date, to_date)
    return _respond(db, user, company_id, "custom-fields",
                    custom_fields.register(db, company_id, f, t, voucher_type), format)


@router.post("/custom-fields/refresh-from-tally")
def custom_fields_refresh(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                          to_date: date | None = None, from_books_beginning: bool = True):
    """Read the custom fields of the vouchers again from Tally's Day Book (read only), in the background.
    By default from the books' beginning (the company's books-from date) to today."""
    from app.core.db import SessionLocal
    from app.services.sync import custom_fields
    company = authorize(db, user, company_id, Perm.SYNC_READ)
    f, t = _default_period(db, company_id, from_date, to_date)
    if from_books_beginning:
        f, t = company.books_from, max(t, date.today())
    if db.get(TallyConnector, company_id) is None:
        raise HTTPException(422, "This company is not connected to Tally")
    job = custom_fields.start_refresh(SessionLocal, company_id, f, t)
    audit.record(db, actor_of(user), "custom_fields.refresh_from_tally", "company", company_id, company_id=company_id,
                 after={"from": str(f), "to": str(t)})
    db.commit()
    return job


@router.get("/custom-fields/refresh-from-tally")
def custom_fields_refresh_status(company_id: uuid.UUID, db: DB, user: CurrentUser):
    from app.services.sync import custom_fields
    authorize(db, user, company_id, Perm.SYNC_READ)
    return custom_fields.job(company_id) or {"running": False, "days": None}


@router.post("/gst/refresh-from-tally")
def gst_refresh(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                to_date: date | None = None):
    """Re-read e-invoice (IRN, acknowledgement) and e-way bill details of the period's invoices from Tally (read only)."""
    from app.services.sync import gst_details
    authorize(db, user, company_id, Perm.SYNC_READ)
    f, t = _default_period(db, company_id, from_date, to_date)
    try:
        r = gst_details.refresh(db, company_id, f, t)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except TallyError as exc:
        raise HTTPException(502, f"Tally: {exc.message}")
    audit.record(db, actor_of(user), "gst.refresh_from_tally", "company", company_id, company_id=company_id,
                 after={"from": str(f), "to": str(t), **r})
    db.commit()
    return r


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


@router.get("/sales-group-units")
def sales_group_units(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                      to_date: date | None = None, party: uuid.UUID | None = None, category: str | None = None,
                      stock_group: uuid.UUID | None = None, voucher_types: str | None = None,
                      include_returns: bool = True, format: Fmt = "json"):
    """Item group-wise sales in the main and the alternate unit, each with its sale rate and FIFO cost rate."""
    authorize(db, user, company_id, Perm.report("sales-group-units"))
    f, t = _default_period(db, company_id, from_date, to_date)
    types = [x.strip() for x in voucher_types.split(",") if x.strip()] if voucher_types else None
    data = analysis.group_wise(db, company_id, "sales", f, t, party=party, category=category, stock_group=stock_group,
                               voucher_types=types, include_returns=include_returns, units=True)
    return _respond(db, user, company_id, "sales-group-units", data, format)


SJ_HEAD = ["Item", "Item group", "Qty", "Unit", "Rate", "Amount", "Godown", "Batch"]
MT_HEAD = ["Item", "Item group", "Qty", "Rate", "Amount", "Godown", "Batch"]   # quantity shown with its unit: "1 Nos"
MTM_HEAD = ["Item", "Item group", "Main group", "Qty", "Rate", "Amount", "Godown", "Batch"]   # main group: the one under Primary


def stock_journal_grid(data: dict, compact: bool = False, main_group: bool = False) -> list[list]:
    """Each voucher as in Tally's stock journal: what came in on the left, what went out on the right.
    compact: seven columns a side with the unit inside the quantity, as the Material Transfer to WIP sheet."""
    head = MTM_HEAD if main_group else MT_HEAD if compact else SJ_HEAD
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
        if main_group:
            return [x["item"], x["item_group"], x["main_group"], qty_text(x["quantity"], x["unit"]), num(x["rate"]),
                    num(x["amount"]), x["godown"], x["batch"]]
        if compact:
            return [x["item"], x["item_group"], qty_text(x["quantity"], x["unit"]), num(x["rate"]), num(x["amount"]),
                    x["godown"], x["batch"]]
        return [x["item"], x["item_group"], num(x["quantity"]), x["unit"], num(x["rate"]), num(x["amount"]), x["godown"], x["batch"]]

    def tot(t):
        if main_group:
            return [("Total", "bold"), None, None, (qty_text(t["quantity"], t["unit"]), "bold"), None,
                    num(t["amount"], "boldnum"), None, None]
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
    return _material_transfer(db, user, company_id, from_date, to_date, item, stock_group, format, "material-transfer-wip")


@router.get("/material-transfer-wip-main")
def material_transfer_wip_main(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                               to_date: date | None = None, item: uuid.UUID | None = None,
                               stock_group: uuid.UUID | None = None, format: Fmt = "json"):
    """Material Transfer to WIP with each item's main group (the group under Primary) after its item group."""
    return _material_transfer(db, user, company_id, from_date, to_date, item, stock_group, format,
                              "material-transfer-wip-main")


def _material_transfer(db, user, company_id, from_date, to_date, item, stock_group, format, report):
    main = report == "material-transfer-wip-main"
    authorize(db, user, company_id, Perm.report(report))
    f, t = _default_period(db, company_id, from_date, to_date)
    data = analysis.stock_journal_register(db, company_id, f, t, voucher_type=analysis.DEFAULT_JOURNAL, item=item,
                                           stock_group=stock_group)
    data["rows"] = [{"date": v["date"], "voucher_number": v["voucher_number"],
                     "direction": "In (produced / received)" if side == "in" else "Out (consumed / issued)",
                     **{k: x[k] for k in ("item", "item_group", *(("main_group",) if main else ()), "quantity", "unit",
                                          "rate", "amount", "godown", "batch")}}
                    for v in data["vouchers"] for side in ("in", "out") for x in v[side]]
    if format != "xlsx":
        return _respond(db, user, company_id, report, data, format)
    authorize(db, user, company_id, Perm.EXPORT)
    audit.record(db, actor_of(user), "report.export", "report", report, company_id=company_id,
                 after={"format": "xlsx", "rows": len(data["rows"])})
    db.commit()
    widths = [34, 20, 20, 11, 13, 15, 18, 14] if main else [34, 20, 11, 13, 15, 18, 14]
    body = xlsx.build_grid(stock_journal_grid(data, compact=True, main_group=main), "Material Transfer to WIP",
                           widths=widths * 2)
    name = "Material Transfer to WIP (main group)" if main else "Material Transfer to WIP"
    return Response(body, media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{name} {f} to {t}.xlsx"'})


WO_HEAD = ["Voucher no", "Voucher type", "Date", "Produced item", "Batch", "Qty", "Item group", "Consumed item group", "Consumed item", "Qty",
           "Rate", "Amount", "Hydraulic", "COC", "OC"]


def wo_grid(data: dict) -> list[list]:
    """One block per voucher: the produced item on its first line, then what was consumed, item-wise under
    each item group with a subtotal for the group, and the voucher's consumption total."""
    def num(v, style="num"):
        return (Decimal(v) if v not in (None, "") else None, style)

    def qty_text(q, unit):
        return f"{Decimal(q).normalize():f} {unit or ''}".strip() if q is not None else None
    def divs(d, style="num"):
        return [num(d[k], style) if Decimal(d[k]) else None for k in analysis.WO_DIVISIONS]

    def line_divs(x):
        return [num(x["amount"]) if x["division"] == k else None for k in analysis.WO_DIVISIONS]
    grid = [[(f"{' & '.join(analysis.WO_MANUFACTURING)}: {data['from']} to {data['to']}", "bold")], [],
            [(h, "head") for h in WO_HEAD]]
    for v in data["vouchers"]:
        made = v["in"] or [None]
        first = True
        rows = []
        for p in made:
            rows.append([v["voucher_number"] if first else None, v["voucher_type"] if first else None,
                         v["date"] if first else None,
                         *([(p["item"], "bold"), p["batch"], (qty_text(p["quantity"], p["unit"]), "bold"), p["item_group"]]
                           if p else [None] * 4)])
            first = False
        out = []
        for g in v["consumed_groups"]:
            for x in g["items"]:
                out.append([g["group"], x["item"], qty_text(x["quantity"], x["unit"]), num(x["rate"]), num(x["amount"]),
                            *line_divs(x)])
            out.append([(f"Total {g['group']}", "bold"), None, (qty_text(g["quantity"], g["unit"]), "bold"), None,
                        num(g["amount"], "boldnum"), *divs(g["divisions"], "boldnum")])
        n = max(len(rows), len(out))
        for i in range(n):
            left = rows[i] if i < len(rows) else [None] * 7
            grid.append(left + (out[i] if i < len(out) else [None] * 5))
        grid.append([("Total consumed", "bold")] + [None] * (len(WO_HEAD) - 5) + [num(v["out_total"]["amount"], "boldnum")]
                    + divs(v["divisions"], "boldnum"))
        grid.append([])
    t = data["total"]
    total = [("", "head")] * len(WO_HEAD)
    total[0] = ("Period total consumed", "head")
    total[-4] = (Decimal(t["out_amount"]), "headnum")
    for i, k in enumerate(analysis.WO_DIVISIONS):
        total[-3 + i] = (Decimal(t["divisions"][k]), "headnum")
    grid.append(total)
    grid.append([f"{t['vouchers']} vouchers"])
    if Decimal(t["divisions"]["other"]):
        grid.append([f"Not in Hydraulic / COC / OC: {t['divisions']['other']} (set the stock group's division in MIS settings)"])
    return grid


@router.get("/wo-manufacturing")
def wo_manufacturing(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                     to_date: date | None = None, item: uuid.UUID | None = None, stock_group: uuid.UUID | None = None,
                     format: Fmt = "json"):
    """WO Manufacturing vouchers: produced item, quantity and item group, and the consumption item-wise by group."""
    authorize(db, user, company_id, Perm.report("wo-manufacturing"))
    f, t = _default_period(db, company_id, from_date, to_date)
    data = analysis.wo_manufacturing(db, company_id, f, t, item=item, stock_group=stock_group)
    data["rows"] = [{"voucher_number": v["voucher_number"], "voucher_type": v["voucher_type"], "date": v["date"],
                     "produced_item": "; ".join(p["item"] for p in v["in"]),
                     "batch": "; ".join(p["batch"] or "" for p in v["in"]),
                     "produced_quantity": "; ".join(f"{p['quantity']} {p['unit'] or ''}".strip() for p in v["in"]),
                     "produced_item_group": "; ".join(p["item_group"] or "" for p in v["in"]),
                     "consumed_item_group": g["group"], "consumed_item": x["item"], "quantity": x["quantity"],
                     "unit": x["unit"], "rate": x["rate"], "amount": x["amount"],
                     **{k: x["amount"] if x["division"] == k else None for k in analysis.WO_DIVISIONS}}
                    for v in data["vouchers"] for g in v["consumed_groups"] for x in g["items"]]
    if format != "xlsx":
        return _respond(db, user, company_id, "wo-manufacturing", data, format)
    authorize(db, user, company_id, Perm.EXPORT)
    audit.record(db, actor_of(user), "report.export", "report", "wo-manufacturing", company_id=company_id,
                 after={"format": "xlsx", "rows": len(data["rows"])})
    db.commit()
    body = xlsx.build_grid(wo_grid(data), "WO Manufacturing", widths=[18, 26, 11, 34, 16, 10, 22, 24, 38, 10, 13, 15, 14, 14, 14])
    return Response(body, media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="WO Manufacturing {f} to {t}.xlsx"'})


@router.get("/fifo-costing")
def fifo_costing(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                 to_date: date | None = None, item: uuid.UUID | None = None, category: str | None = None,
                 stock_group: uuid.UUID | None = None, format: Fmt = "json"):
    """Opening, inward, outward and closing stock per item, valued first-in-first-out."""
    authorize(db, user, company_id, Perm.report("fifo-costing"))
    f, t = _default_period(db, company_id, from_date, to_date)
    data = analysis.fifo_costing(db, company_id, f, t, item=item, category=category, stock_group=stock_group)
    return _respond(db, user, company_id, "fifo-costing", data, format)


@router.get("/fifo-monthly")
def fifo_monthly(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                 to_date: date | None = None, item: uuid.UUID | None = None, category: str | None = None,
                 stock_group: uuid.UUID | None = None, format: Fmt = "json"):
    """FIFO stock costing month by month: opening, inward, outward and closing, quantity and value."""
    authorize(db, user, company_id, Perm.report("fifo-monthly"))
    f, t = _default_period(db, company_id, from_date, to_date)
    data = analysis.fifo_monthly(db, company_id, f, t, item=item, category=category, stock_group=stock_group)
    return _respond(db, user, company_id, "fifo-monthly", data, format)


@router.get("/godown-summary")
def godown_summary(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                   to_date: date | None = None, item: uuid.UUID | None = None, category: str | None = None,
                   stock_group: uuid.UUID | None = None, format: Fmt = "json"):
    """Stock by store (godown) and item: opening, inward, outward and closing, quantity, rate and value."""
    authorize(db, user, company_id, Perm.report("godown-summary"))
    f, t = _default_period(db, company_id, from_date, to_date)
    data = analysis.godown_summary(db, company_id, f, t, item=item, category=category, stock_group=stock_group)
    return _respond(db, user, company_id, "godown-summary", data, format)


@router.get("/filters")
def report_filters(company_id: uuid.UUID, db: DB, user: CurrentUser):
    """Choices for the analysis filters: parties, items, item categories and stock groups."""
    from sqlalchemy import func
    from app.core.security import has_perm
    from app.models import Ledger, StockGroup, StockItem, Voucher
    allowed = [k for k in ("sales-analysis", "purchase-analysis", "fifo-costing", "fifo-monthly", "godown-summary", "sales-rm-cost", "sales-fifo-cost",
                              "sales-group-wise", "sales-group-units")
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
