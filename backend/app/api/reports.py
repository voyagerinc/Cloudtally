import csv
import io
import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app import audit, reports
from app.api.deps import DB, CurrentUser, actor_of, authorize
from app.core.security import Perm
from app.models import FinancialYear

router = APIRouter(prefix="/reports", tags=["reports"])

# Which list inside each report becomes the CSV rows.
CSV_ROWS = {
    "trial-balance": "lines", "day-book": "vouchers", "ledger": "lines", "stock-summary": "items",
    "outstanding": "bills", "register": "vouchers", "ledger-balances": None, "stock-item": "lines",
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
    if fmt != "csv":
        return data
    authorize(db, user, company_id, Perm.EXPORT)
    rows = data if isinstance(data, list) else data.get(CSV_ROWS.get(name) or "", [])
    audit.record(db, actor_of(user), "report.export", "report", name, company_id=company_id,
                 after={"format": fmt, "rows": len(rows)})
    db.commit()
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


Fmt = Literal["json", "csv"]


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
