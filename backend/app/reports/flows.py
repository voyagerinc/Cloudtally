"""Cash Flow and Funds Flow, in the way Tally shows them.

Cash Flow: the money that came into and went out of the cash and bank accounts in the period, grouped by the
group of the other side of each voucher (e.g. Sundry Debtors, Indirect Expenses), with a month-by-month view.
Transfers between cash and bank accounts (contra) are not a flow. Opening + inflow - outflow = closing.

Funds Flow: where long-term funds came from and went to. Sources are the net profit and the decreases in
long-term assets / increases in long-term liabilities (capital, loans, ...); applications are the reverse.
The difference is the change in working capital (current assets less current liabilities), shown group-wise."""
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Company, Ledger, Voucher, VoucherEntry, VoucherType
from app.models.vouchers import VoucherStatus
from app.reports import _groups, _stock_in_ledgers, _stock_value, ledger_balances, profit_and_loss
from app.services.canonical import m

ZERO = Decimal("0")
CASH_GROUPS = {"Cash-in-Hand", "Bank Accounts", "Bank OD A/c", "Bank OCC A/c"}
WORKING_CAPITAL = {"Current Assets", "Current Liabilities"}


def _is_cash(path: list[str]) -> bool:
    return any(p in CASH_GROUPS for p in path)


def cash_flow(db: Session, company_id: uuid.UUID, from_date: date, to_date: date) -> dict:
    groups = _groups(db, company_id)
    ledgers = {led.id: led for led in db.scalars(select(Ledger).where(Ledger.company_id == company_id))}
    cash_ids = {lid for lid, led in ledgers.items() if _is_cash(groups[led.group_id]["path"])}
    balances = {r["ledger_id"]: r for r in ledger_balances(db, company_id, to_date, from_date)}
    opening = sum((Decimal(balances[str(i)]["opening"]) for i in cash_ids if str(i) in balances), ZERO)
    closing = sum((Decimal(balances[str(i)]["closing"]) for i in cash_ids if str(i) in balances), ZERO)

    inflow: dict[str, dict] = defaultdict(lambda: {"amount": ZERO, "ledgers": defaultdict(lambda: ZERO)})
    outflow: dict[str, dict] = defaultdict(lambda: {"amount": ZERO, "ledgers": defaultdict(lambda: ZERO)})
    months: dict[str, dict] = {}
    stmt = (select(Voucher).join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
            .where(Voucher.company_id == company_id, Voucher.status == VoucherStatus.POSTED,
                   Voucher.voucher_date.between(from_date, to_date), VoucherType.affects_accounts.is_(True),
                   Voucher.id.in_(select(VoucherEntry.voucher_id).where(VoucherEntry.ledger_id.in_(cash_ids))))
            .options(selectinload(Voucher.entries)).order_by(Voucher.voucher_date))
    for v in db.scalars(stmt) if cash_ids else []:
        cash = sum((e.amount for e in v.entries if e.ledger_id in cash_ids), ZERO)    # Dr: money in
        others = [e for e in v.entries if e.ledger_id not in cash_ids and e.amount]
        if cash == ZERO or not others:
            continue                                                             # contra: cash to bank
        month = v.voucher_date.strftime("%Y-%m")
        mo = months.setdefault(month, {"month": month, "inflow": ZERO, "outflow": ZERO})
        # The cash amount, split over the other side's lines (one line in most vouchers).
        total = sum((-e.amount for e in others), ZERO)
        for e in others:
            share = (-e.amount) * cash / total if total else ZERO
            if share == ZERO:
                continue
            led = ledgers[e.ledger_id]
            path = groups[led.group_id]["path"]          # e.g. Current Assets > Sundry Debtors: shown as Sundry Debtors
            group = path[1] if len(path) > 1 else path[0]
            side = inflow if share > 0 else outflow
            side[group]["amount"] += abs(share)
            side[group]["ledgers"][led.name] += abs(share)
            mo["inflow" if share > 0 else "outflow"] += abs(share)

    def fmt(side):
        return [{"group": g, "amount": m(x["amount"]),
                 "ledgers": [{"ledger": n, "amount": m(a)} for n, a in sorted(x["ledgers"].items(), key=lambda kv: -kv[1])]}
                for g, x in sorted(side.items(), key=lambda kv: -kv[1]["amount"])]
    total_in = sum((x["amount"] for x in inflow.values()), ZERO)
    total_out = sum((x["amount"] for x in outflow.values()), ZERO)
    return {"from": str(from_date), "to": str(to_date), "opening": m(opening), "closing": m(closing),
            "inflow": fmt(inflow), "outflow": fmt(outflow), "total_inflow": m(total_in), "total_outflow": m(total_out),
            "net_flow": m(total_in - total_out), "difference": m(opening + total_in - total_out - closing),
            "rows": [{"flow": side_name, "group": g["group"], "ledger": led["ledger"], "amount": led["amount"]}
                     for side_name, side in (("Inflow", fmt(inflow)), ("Outflow", fmt(outflow)))
                     for g in side for led in g["ledgers"]],
            "months": [{"month": k, "inflow": m(x["inflow"]), "outflow": m(x["outflow"]),
                        "net": m(x["inflow"] - x["outflow"])} for k, x in sorted(months.items())],
            "cash_ledgers": sorted(ledgers[i].name for i in cash_ids)}


def funds_flow(db: Session, company_id: uuid.UUID, from_date: date, to_date: date) -> dict:
    rows = [r for r in ledger_balances(db, company_id, to_date, from_date) if not r["is_revenue"]]
    stock_ledgers = _stock_in_ledgers(rows)
    wc_open, wc_close = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)    # Dr positive
    change: dict[str, Decimal] = defaultdict(lambda: ZERO)                         # long-term groups, Dr positive
    for r in rows:
        primary = r["path"][0] if r["path"] else r["primary_group"]
        opening, closing = Decimal(r["opening"]), Decimal(r["closing"])
        if primary in WORKING_CAPITAL or _is_cash(r["path"]):
            name = r["path"][1] if len(r["path"]) > 1 else primary
            wc_open[name] += opening
            wc_close[name] += closing
        else:
            change[primary] += closing - opening
    if not stock_ledgers:   # integrated books: stock is valued from the items
        wc_open["Stock-in-Hand"] += _stock_value(db, company_id, from_date - timedelta(days=1))
        wc_close["Stock-in-Hand"] += _stock_value(db, company_id, to_date)
    profit = Decimal(profit_and_loss(db, company_id, from_date, to_date)["net_profit"])

    sources, applications = [], []
    if profit > 0:
        sources.append({"particulars": "Net profit", "amount": profit})
    elif profit < 0:
        applications.append({"particulars": "Net loss", "amount": -profit})
    for group, delta in sorted(change.items()):
        if delta == ZERO:
            continue
        # Less debit / more credit in a long-term group brings funds in (a loan taken, an asset sold).
        (sources if delta < 0 else applications).append({"particulars": group, "amount": abs(delta)})
    total_src = sum((x["amount"] for x in sources), ZERO)
    total_app = sum((x["amount"] for x in applications), ZERO)
    wc = [{"group": g, "opening": m(wc_open[g]), "closing": m(wc_close[g]), "change": m(wc_close[g] - wc_open[g])}
          for g in sorted(set(wc_open) | set(wc_close)) if wc_open[g] or wc_close[g]]
    wc_change = sum(wc_close.values(), ZERO) - sum(wc_open.values(), ZERO)
    fmt = lambda xs: [{"particulars": x["particulars"], "amount": m(x["amount"])} for x in xs]   # noqa: E731
    return {"from": str(from_date), "to": str(to_date), "sources": fmt(sources), "applications": fmt(applications),
            "total_sources": m(total_src), "total_applications": m(total_app),
            "working_capital_change": m(total_src - total_app), "working_capital": wc,
            "working_capital_opening": m(sum(wc_open.values(), ZERO)), "working_capital_closing": m(sum(wc_close.values(), ZERO)),
            "difference": m(total_src - total_app - wc_change), "net_profit": m(profit),
            "rows": [{"side": "Sources", **x} for x in fmt(sources)] + [{"side": "Applications", **x} for x in fmt(applications)]
                    + [{"side": "Working capital", "particulars": w["group"], "amount": w["change"]} for w in wc],
            "company": db.get(Company, company_id).name}
