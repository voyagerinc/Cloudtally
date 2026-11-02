"""Financial reports computed from posted vouchers only (PRD §5.4).

Signs: balances are debit-positive. Display helpers split them into Dr/Cr columns.
"""
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    BillAllocation, Company, Ledger, LedgerGroup, StockGroup, StockItem, Voucher, VoucherEntry, VoucherInventoryLine,
    VoucherStatus, VoucherType,
)
from app.services.canonical import m

ZERO = Decimal("0")
POSTED = VoucherStatus.POSTED


# ------------------------------------------------------------------ helpers

def _groups(db: Session, company_id: uuid.UUID) -> dict[uuid.UUID, dict]:
    rows = db.scalars(select(LedgerGroup).where(LedgerGroup.company_id == company_id)).all()
    by_id = {g.id: g for g in rows}
    out = {}
    for g in rows:
        primary, path, seen = g, [g.name], {g.id}
        while primary.parent_id and primary.parent_id in by_id and primary.parent_id not in seen:
            primary = by_id[primary.parent_id]
            seen.add(primary.id)
            path.append(primary.name)
        out[g.id] = {"group": g, "primary": primary, "path": list(reversed(path))}
    return out


def _dr_cr(balance: Decimal) -> dict:
    return {"debit": m(balance) if balance > 0 else "0.00", "credit": m(-balance) if balance < 0 else "0.00"}


def _movements(db: Session, company_id, from_date: date | None, to_date: date):
    debit = func.sum(case((VoucherEntry.amount > 0, VoucherEntry.amount), else_=0))
    credit = func.sum(case((VoucherEntry.amount < 0, -VoucherEntry.amount), else_=0))
    stmt = (select(VoucherEntry.ledger_id, debit, credit)
            .join(Voucher, Voucher.id == VoucherEntry.voucher_id).join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
            .where(Voucher.company_id == company_id, Voucher.status == POSTED, Voucher.voucher_date <= to_date,
                   VoucherType.affects_accounts.is_(True))
            .group_by(VoucherEntry.ledger_id))
    if from_date:
        stmt = stmt.where(Voucher.voucher_date >= from_date)
    return {lid: (d or ZERO, c or ZERO) for lid, d, c in db.execute(stmt)}


def ledger_balances(db: Session, company_id: uuid.UUID, to_date: date, from_date: date | None = None) -> list[dict]:
    groups = _groups(db, company_id)
    ledgers = db.scalars(select(Ledger).where(Ledger.company_id == company_id).order_by(Ledger.name)).all()
    period = _movements(db, company_id, from_date, to_date)
    before = _movements(db, company_id, None, from_date - timedelta(days=1)) if from_date else {}
    rows = []
    for led in ledgers:
        g = groups[led.group_id]
        revenue = g["group"].is_revenue
        # Revenue ledgers restart each period; balance-sheet ledgers carry forward.
        prior = before.get(led.id, (ZERO, ZERO))
        opening = ZERO if (revenue and from_date) else led.opening_balance + prior[0] - prior[1]
        debit, credit = period.get(led.id, (ZERO, ZERO))
        closing = opening + debit - credit
        rows.append({
            "ledger_id": str(led.id), "ledger": led.name, "group": g["group"].name, "primary_group": g["primary"].name,
            "path": g["path"],
            "nature": g["group"].nature, "is_revenue": revenue, "affects_gross_profit": g["group"].affects_gross_profit,
            "opening": m(opening), "debit": m(debit), "credit": m(credit), "closing": m(closing),
        })
    return rows


# ------------------------------------------------------------------ trial balance

def trial_balance(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, *, by: str = "ledger") -> dict:
    rows = [r for r in ledger_balances(db, company_id, to_date, from_date)
            if Decimal(r["closing"]) or Decimal(r["debit"]) or Decimal(r["credit"]) or Decimal(r["opening"])]
    if by == "group":
        agg = defaultdict(lambda: ZERO)
        for r in rows:
            agg[r["primary_group"]] += Decimal(r["closing"])
        lines = [{"name": k, **_dr_cr(v)} for k, v in sorted(agg.items()) if v]
    else:
        lines = [{"name": r["ledger"], "ledger_id": r["ledger_id"], "group": r["group"], **_dr_cr(Decimal(r["closing"]))}
                 for r in rows if Decimal(r["closing"])]
    total_dr = sum((Decimal(x["debit"]) for x in lines), ZERO)
    total_cr = sum((Decimal(x["credit"]) for x in lines), ZERO)
    opening_diff = sum((led.opening_balance for led in db.scalars(select(Ledger).where(Ledger.company_id == company_id))), ZERO)
    return {"from": str(from_date), "to": str(to_date), "lines": lines, "total_debit": m(total_dr),
            "total_credit": m(total_cr), "difference": m(total_dr - total_cr),
            "opening_balance_difference": m(opening_diff)}


# ------------------------------------------------------------------ stock

def stock_summary(db: Session, company_id: uuid.UUID, to_date: date, from_date: date | None = None) -> dict:
    items = db.scalars(select(StockItem).where(StockItem.company_id == company_id).order_by(StockItem.name)).all()
    # Value-only lines (quantity 0) change the value but not the quantity, in their stated direction.
    is_in = or_(VoucherInventoryLine.quantity > 0, and_(VoucherInventoryLine.quantity == 0, VoucherInventoryLine.inward.is_(True)))
    is_out = or_(VoucherInventoryLine.quantity < 0, and_(VoucherInventoryLine.quantity == 0, VoucherInventoryLine.inward.is_(False)))
    inward_q = func.sum(case((VoucherInventoryLine.quantity > 0, VoucherInventoryLine.quantity), else_=0))
    inward_v = func.sum(case((is_in, VoucherInventoryLine.amount), else_=0))
    outward_q = func.sum(case((VoucherInventoryLine.quantity < 0, -VoucherInventoryLine.quantity), else_=0))
    outward_v = func.sum(case((is_out, VoucherInventoryLine.amount), else_=0))

    def moves(start, end):
        stmt = (select(VoucherInventoryLine.stock_item_id, inward_q, inward_v, outward_q, outward_v)
                .join(Voucher).join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
                .where(Voucher.company_id == company_id, Voucher.status == POSTED, Voucher.voucher_date <= end,
                       VoucherType.affects_stock.is_(True))
                .group_by(VoucherInventoryLine.stock_item_id))
        if start:
            stmt = stmt.where(Voucher.voucher_date >= start)
        return {r[0]: [x or ZERO for x in r[1:]] for r in db.execute(stmt)}

    period = moves(from_date, to_date)
    before = moves(None, from_date - timedelta(days=1)) if from_date else {}
    paths = _stock_group_paths(db, company_id)
    rows, total_value = [], ZERO
    for it in items:
        b = before.get(it.id, [ZERO] * 4)
        p = period.get(it.id, [ZERO] * 4)
        open_qty = it.opening_qty + b[0] - b[2]
        cost_qty = it.opening_qty + b[0] + p[0]
        cost_val = it.opening_value + b[1] + p[1]
        avg = (cost_val / cost_qty) if cost_qty else it.opening_rate
        close_qty = open_qty + p[0] - p[2]
        value = (close_qty * avg).quantize(Decimal("0.01"))
        total_value += value
        if open_qty or p[0] or p[2] or close_qty:
            rows.append({"item_id": str(it.id), "item": it.name, "path": paths.get(it.group_id, []),
                         "unit": it.unit, "opening_qty": str(open_qty), "inward_qty": str(p[0]),
                         "inward_value": m(p[1]), "outward_qty": str(p[2]), "outward_value": m(p[3]),
                         "closing_qty": str(close_qty), "rate": m(avg), "closing_value": m(value)})
    return {"to": str(to_date), "items": rows, "total_value": m(total_value)}


def _stock_group_paths(db: Session, company_id) -> dict:
    """Stock group id -> names from the top group down to it."""
    groups = {g.id: g for g in db.scalars(select(StockGroup).where(StockGroup.company_id == company_id))}
    out = {}
    for gid, g in groups.items():
        path, seen = [g.name], {gid}
        while g.parent_id in groups and g.parent_id not in seen:
            g = groups[g.parent_id]
            seen.add(g.id)
            path.append(g.name)
        out[gid] = list(reversed(path))
    return out


def stock_item_movement(db: Session, company_id: uuid.UUID, item_id: uuid.UUID, from_date: date, to_date: date) -> dict:
    """Every posted voucher that moved one item, with a running quantity (Tally's Stock Item Vouchers)."""
    item = db.get(StockItem, item_id)
    if item is None or item.company_id != company_id:
        raise LookupError("Stock item not found")
    summary = next((r for r in stock_summary(db, company_id, to_date, from_date)["items"] if r["item_id"] == str(item_id)), None)
    opening = Decimal(summary["opening_qty"]) if summary else item.opening_qty
    stmt = (select(VoucherInventoryLine, Voucher).join(Voucher).join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
            .where(Voucher.company_id == company_id, Voucher.status == POSTED, VoucherType.affects_stock.is_(True),
                   VoucherInventoryLine.stock_item_id == item_id, Voucher.voucher_date.between(from_date, to_date))
            .order_by(Voucher.voucher_date, Voucher.created_at, VoucherInventoryLine.line_no))
    running, lines = opening, []
    for line, v in db.execute(stmt):
        inward = line.quantity > 0 or (line.quantity == 0 and line.inward)
        running += line.quantity
        party = v.party_ledger.name if v.party_ledger else next((e.ledger.name for e in v.entries), None)
        lines.append({"voucher_id": str(v.id), "date": str(v.voucher_date), "voucher_type": v.voucher_type.name,
                      "voucher_number": v.voucher_number, "particulars": party, "godown": line.godown,
                      "inward_qty": str(line.quantity) if line.quantity > 0 else "", "outward_qty": str(-line.quantity) if line.quantity < 0 else "",
                      "inward_value": m(line.amount) if inward else "", "outward_value": "" if inward else m(line.amount),
                      "rate": m(line.rate), "balance_qty": str(running)})
    return {"item": item.name, "unit": item.unit, "from": str(from_date), "to": str(to_date), "opening_qty": str(opening),
            "lines": lines, "closing_qty": str(running), "closing_value": summary["closing_value"] if summary else "0.00"}


def _stock_value(db: Session, company_id, as_of: date | None) -> Decimal:
    if as_of is None:
        return sum((i.opening_value for i in db.scalars(select(StockItem).where(StockItem.company_id == company_id))), ZERO)
    return Decimal(stock_summary(db, company_id, as_of)["total_value"])


# ------------------------------------------------------------------ P&L and balance sheet

STOCK_GROUP = "Stock-in-Hand"


def _stock_in_ledgers(rows: list[dict]) -> bool:
    """Non-integrated books: stock is carried in Stock-in-Hand ledgers, not valued from items (as Tally does)."""
    return any(STOCK_GROUP in r["path"] and (Decimal(r["opening"]) or Decimal(r["closing"])) for r in rows)


def profit_and_loss(db: Session, company_id: uuid.UUID, from_date: date, to_date: date) -> dict:
    all_rows = ledger_balances(db, company_id, to_date, from_date)
    rows = [r for r in all_rows if r["is_revenue"]]
    stock_ledgers = _stock_in_ledgers(all_rows)
    if stock_ledgers:
        # Stock ledgers are balance-sheet items; they carry the stock value already, so no P&L adjustment.
        opening_stock = closing_stock = ZERO
    else:
        opening_stock = _stock_value(db, company_id, from_date - timedelta(days=1)) if from_date else ZERO
        closing_stock = _stock_value(db, company_id, to_date)

    def section(gp: bool, nature: str):
        agg = defaultdict(lambda: ZERO)
        for r in rows:
            if r["affects_gross_profit"] == gp and r["nature"] == nature:
                agg[r["primary_group"]] += Decimal(r["closing"])
        sign = 1 if nature == "expense" else -1
        return {k: v * sign for k, v in agg.items() if v}

    trading_exp, trading_inc = section(True, "expense"), section(True, "income")
    indirect_exp, indirect_inc = section(False, "expense"), section(False, "income")
    gross = sum(trading_inc.values(), ZERO) + closing_stock - opening_stock - sum(trading_exp.values(), ZERO)
    net = gross + sum(indirect_inc.values(), ZERO) - sum(indirect_exp.values(), ZERO)
    fmt = lambda d: [{"group": k, "amount": m(v)} for k, v in sorted(d.items())]   # noqa: E731
    return {"from": str(from_date), "to": str(to_date), "opening_stock": m(opening_stock),
            "closing_stock": m(closing_stock), "direct_expenses": fmt(trading_exp), "direct_incomes": fmt(trading_inc),
            "gross_profit": m(gross), "indirect_expenses": fmt(indirect_exp), "indirect_incomes": fmt(indirect_inc),
            "net_profit": m(net), "stock_source": "ledgers" if stock_ledgers else "items"}


def balance_sheet(db: Session, company_id: uuid.UUID, as_of: date) -> dict:
    company = db.get(Company, company_id)
    rows = ledger_balances(db, company_id, as_of)
    liabilities, assets = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    for r in rows:
        if r["is_revenue"]:
            continue
        bal = Decimal(r["closing"])
        if r["nature"] == "asset":
            assets[r["primary_group"]] += bal
        else:
            liabilities[r["primary_group"]] -= bal
    pl = profit_and_loss(db, company_id, company.books_from, as_of)
    closing_stock = Decimal(pl["closing_stock"])
    if closing_stock:
        assets["Closing Stock"] += closing_stock
    liabilities["Profit & Loss A/c"] += Decimal(pl["net_profit"])
    opening_diff = sum((led.opening_balance for led in db.scalars(select(Ledger).where(Ledger.company_id == company_id))), ZERO)
    if opening_diff:
        liabilities["Difference in Opening Balances"] += opening_diff
    total_l = sum(liabilities.values(), ZERO)
    total_a = sum(assets.values(), ZERO)
    fmt = lambda d: [{"group": k, "amount": m(v)} for k, v in sorted(d.items()) if v]   # noqa: E731
    return {"as_of": str(as_of), "liabilities": fmt(liabilities), "assets": fmt(assets),
            "total_liabilities": m(total_l), "total_assets": m(total_a), "difference": m(total_a - total_l)}


# ------------------------------------------------------------------ books

def day_book(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, *, voucher_type: str | None = None,
             include_unposted: bool = True) -> dict:
    stmt = (select(Voucher).where(Voucher.company_id == company_id, Voucher.voucher_date.between(from_date, to_date),
                                  Voucher.status != VoucherStatus.DELETED)
            .order_by(Voucher.voucher_date, Voucher.created_at))
    if voucher_type:
        stmt = stmt.join(VoucherType).where(VoucherType.name == voucher_type)
    if not include_unposted:
        stmt = stmt.where(Voucher.status == POSTED)
    rows, total = [], ZERO
    for v in db.scalars(stmt):
        amount = sum((e.amount for e in v.entries if e.amount > 0), ZERO)
        if v.status == POSTED:
            total += amount
        rows.append({"id": str(v.id), "date": str(v.voucher_date), "voucher_type": v.voucher_type.name,
                     "voucher_number": v.voucher_number, "party": v.party_ledger.name if v.party_ledger else
                     (v.entries[0].ledger.name if v.entries else None), "narration": v.narration,
                     "amount": m(amount), "status": v.status, "sync_status": v.sync_status, "source": v.source_system})
    return {"from": str(from_date), "to": str(to_date), "vouchers": rows, "posted_total": m(total)}


def ledger_statement(db: Session, company_id: uuid.UUID, ledger_id: uuid.UUID, from_date: date, to_date: date) -> dict:
    led = db.get(Ledger, ledger_id)
    if led is None or led.company_id != company_id:
        raise LookupError("Ledger not found")
    bal = next(r for r in ledger_balances(db, company_id, to_date, from_date) if r["ledger_id"] == str(led.id))
    running = Decimal(bal["opening"])
    stmt = (select(VoucherEntry, Voucher).join(Voucher, Voucher.id == VoucherEntry.voucher_id)
            .join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
            .where(Voucher.company_id == company_id, Voucher.status == POSTED, VoucherEntry.ledger_id == led.id,
                   VoucherType.affects_accounts.is_(True),
                   Voucher.voucher_date.between(from_date, to_date))
            .order_by(Voucher.voucher_date, Voucher.created_at, VoucherEntry.line_no))
    lines = []
    for entry, v in db.execute(stmt):
        running += entry.amount
        others = [e.ledger.name for e in v.entries if e.ledger_id != led.id]
        lines.append({"voucher_id": str(v.id), "date": str(v.voucher_date), "voucher_type": v.voucher_type.name,
                      "voucher_number": v.voucher_number, "particulars": others[0] if len(others) == 1 else
                      ("(as per details)" if others else led.name), "narration": entry.narration or v.narration,
                      **_dr_cr(entry.amount), "balance": m(running)})
    return {"ledger": led.name, "group": led.group.name, "from": str(from_date), "to": str(to_date),
            "opening": m(Decimal(bal["opening"])), "lines": lines, "total_debit": bal["debit"],
            "total_credit": bal["credit"], "closing": bal["closing"]}


def outstanding(db: Session, company_id: uuid.UUID, as_of: date, kind: str = "receivable") -> dict:
    """Bill-wise outstanding with ageing buckets for Sundry Debtors (receivable) or Creditors (payable)."""
    root_name = "Sundry Debtors" if kind == "receivable" else "Sundry Creditors"
    groups = _groups(db, company_id)
    wanted = {gid for gid, g in groups.items() if root_name in g["path"]}
    ledgers = {led.id: led for led in db.scalars(select(Ledger).where(Ledger.company_id == company_id,
                                                                     Ledger.group_id.in_(wanted)))}
    stmt = (select(VoucherEntry.ledger_id, BillAllocation.name, BillAllocation.ref_type, BillAllocation.amount,
                   BillAllocation.due_date, Voucher.voucher_date, Voucher.id)
            .join(VoucherEntry, VoucherEntry.id == BillAllocation.entry_id)
            .join(Voucher, Voucher.id == VoucherEntry.voucher_id)
            .join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
            .where(Voucher.company_id == company_id, Voucher.status == POSTED, Voucher.voucher_date <= as_of,
                   VoucherType.affects_accounts.is_(True), VoucherEntry.ledger_id.in_(ledgers.keys())))
    bills: dict[tuple, dict] = {}
    for lid, name, ref_type, amount, due, vdate, vid in db.execute(stmt):
        key = (lid, name or f"({ref_type})")
        b = bills.setdefault(key, {"amount": ZERO, "date": vdate, "due": due, "voucher_id": vid})
        b["amount"] += amount
        if ref_type in ("New Ref", "Advance") and vdate <= b["date"]:
            b["date"], b["due"], b["voucher_id"] = vdate, due or b["due"], vid
    sign = 1 if kind == "receivable" else -1
    buckets = ("0-30", "31-60", "61-90", "90+")
    rows, totals = [], dict.fromkeys(buckets, ZERO)
    for (lid, name), b in sorted(bills.items(), key=lambda kv: (ledgers[kv[0][0]].name, kv[1]["date"])):
        pending = b["amount"] * sign
        if pending == ZERO:
            continue
        led = ledgers[lid]
        due = b["due"] or (b["date"] + timedelta(days=led.credit_days or 0))
        overdue = (as_of - due).days
        age = (as_of - b["date"]).days
        bucket = buckets[0] if age <= 30 else buckets[1] if age <= 60 else buckets[2] if age <= 90 else buckets[3]
        totals[bucket] += pending
        rows.append({"party": led.name, "ledger_id": str(lid), "voucher_id": str(b["voucher_id"]),
                     "bill": name, "date": str(b["date"]), "due_date": str(due),
                     "pending": m(pending), "age_days": age, "overdue_days": max(0, overdue), "bucket": bucket})
    return {"as_of": str(as_of), "kind": kind, "bills": rows, "ageing": {k: m(v) for k, v in totals.items()},
            "total": m(sum(totals.values(), ZERO))}


def register(db: Session, company_id: uuid.UUID, base_type: str, from_date: date, to_date: date) -> dict:
    stmt = (select(Voucher).join(VoucherType)
            .where(Voucher.company_id == company_id, Voucher.status == POSTED, VoucherType.base_type == base_type,
                   Voucher.voucher_date.between(from_date, to_date)).order_by(Voucher.voucher_date))
    rows, total = [], ZERO
    for v in db.scalars(stmt):
        amount = sum((e.amount for e in v.entries if e.amount > 0), ZERO)
        tax = sum((abs(e.amount) for e in v.entries if e.ledger.tax_type or e.ledger.gst_duty_head), ZERO)
        total += amount
        rows.append({"id": str(v.id), "date": str(v.voucher_date), "voucher_number": v.voucher_number,
                     "voucher_type": v.voucher_type.name, "party": v.party_ledger.name if v.party_ledger else None,
                     "gstin": v.party_ledger.gstin if v.party_ledger else None, "amount": m(amount), "tax": m(tax)})
    return {"base_type": base_type, "from": str(from_date), "to": str(to_date), "vouchers": rows, "total": m(total)}
