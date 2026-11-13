"""Sales and purchase analysis, FIFO costing, and raw-material cost of sales from production vouchers."""
import bisect
import re
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.models import Ledger, StockGroup, StockItem, Voucher, VoucherInventoryLine, VoucherType
from app.services.canonical import m

ZERO = Decimal("0")
POSTED = "posted"
BY = ("party", "date", "month", "item", "category", "stock_group", "voucher")
SIDES = {"sales": ("Sales", "Credit Note"), "purchase": ("Purchase", "Debit Note")}
# Shown but not counted by default: Tally keeps these as memos, not as sales or purchases.
MEMO_TYPE = re.compile(r"proforma|quotation|estimate", re.I)


def _qty(q: Decimal) -> str:
    """1200, 0.6 or 12.5: plain digits, never 1.2E+3."""
    return format(q.normalize(), "f") if q else "0"


def _inward(line: VoucherInventoryLine) -> bool:
    return line.quantity > 0 if line.quantity != 0 else bool(line.inward)


def _group_names(db: Session, company_id) -> dict:
    return {g.id: g.name for g in db.scalars(select(StockGroup).where(StockGroup.company_id == company_id))}


# ------------------------------------------------------------------ raw-material cost from production vouchers

def production_costs(db: Session, company_id: uuid.UUID, to_date: date) -> dict[uuid.UUID, list]:
    """Per finished item, its production up to `to_date`: sorted [(date, quantity, raw material value)].

    A production voucher is a Stock Journal-type voucher that books items in from other items going out
    (Manufacturing Journal, work-order vouchers). Transfers that move the same item in and out are not.
    The components' value is shared among the produced items by their value (Tally's own split); extra
    costs on the voucher (labour, power) are not raw material and are left out."""
    rows = db.execute(
        select(VoucherInventoryLine, Voucher.voucher_date, Voucher.id)
        .join(Voucher, Voucher.id == VoucherInventoryLine.voucher_id).join(VoucherType)
        .where(Voucher.company_id == company_id, Voucher.status == POSTED, VoucherType.base_type == "Stock Journal",
               Voucher.voucher_date <= to_date)).all()
    by_voucher: dict[uuid.UUID, list] = defaultdict(list)
    dates = {}
    for line, vdate, vid in rows:
        by_voucher[vid].append(line)
        dates[vid] = vdate
    out: dict[uuid.UUID, list] = defaultdict(list)
    for vid, lines in by_voucher.items():
        ins = [l for l in lines if _inward(l) and l.quantity > 0]
        outs = [l for l in lines if not _inward(l)]
        in_items = {l.stock_item_id for l in ins}
        out_items = {l.stock_item_id for l in outs}
        components = [l for l in outs if l.stock_item_id not in in_items]
        produced = [l for l in ins if l.stock_item_id not in out_items]
        if not components or not produced:
            continue                                  # a transfer between godowns, not production
        material = sum((l.amount for l in components), ZERO)
        weight = sum((l.amount for l in produced), ZERO)
        qty_total = sum((l.quantity for l in produced), ZERO)
        for l in produced:
            share = (l.amount / weight) if weight else (l.quantity / qty_total)
            out[l.stock_item_id].append((dates[vid], l.quantity, material * share))
    for v in out.values():
        v.sort(key=lambda r: r[0])
    return out


class RawMaterialCost:
    """Weighted-average raw-material cost per unit of an item, from its production up to a date."""

    def __init__(self, production: dict[uuid.UUID, list]):
        self.dates, self.cum_qty, self.cum_val = {}, {}, {}
        for item, rows in production.items():
            q = v = ZERO
            self.dates[item], self.cum_qty[item], self.cum_val[item] = [], [], []
            for d, qty, val in rows:
                q, v = q + qty, v + val
                self.dates[item].append(d)
                self.cum_qty[item].append(q)
                self.cum_val[item].append(v)

    def per_unit(self, item: uuid.UUID, on: date) -> Decimal | None:
        ds = self.dates.get(item)
        if not ds:
            return None
        i = bisect.bisect_right(ds, on) - 1
        if i < 0:
            i = 0          # sold before it was first produced in these books: use its first production
        q = self.cum_qty[item][i]
        return self.cum_val[item][i] / q if q else None


# ------------------------------------------------------------------ sales / purchase analysis

def trade_analysis(db: Session, company_id: uuid.UUID, side: str, from_date: date, to_date: date, *,
                   by: str = "party", party: uuid.UUID | None = None, item: uuid.UUID | None = None,
                   category: str | None = None, stock_group: uuid.UUID | None = None,
                   voucher_types: list[str] | None = None, include_returns: bool = True,
                   cost: str | None = None) -> dict:
    """Item lines of sales (or purchases) in the period, summed by party, date, month, item, category or
    stock group, or listed per voucher. Returns (credit/debit notes) count negative.

    cost: "rm" adds the raw-material cost from production vouchers, "fifo" the FIFO cost of the goods
    sold, each with the margin."""
    if by not in BY:
        raise ValueError(f"by must be one of {', '.join(BY)}")
    main, ret = SIDES[side]
    bases = (main, ret) if include_returns else (main,)
    types = list(db.scalars(select(VoucherType).where(VoucherType.company_id == company_id,
                                                      VoucherType.base_type.in_((main, ret)))))
    if voucher_types:
        chosen = {t.id for t in types if t.name in set(voucher_types)}
    else:
        chosen = {t.id for t in types if t.base_type in bases and not MEMO_TYPE.search(t.name)}
    party_l = aliased(Ledger)
    rows = db.execute(
        select(VoucherInventoryLine, Voucher, VoucherType.base_type, VoucherType.name, StockItem, party_l.name)
        .join(Voucher, Voucher.id == VoucherInventoryLine.voucher_id)
        .join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
        .join(StockItem, StockItem.id == VoucherInventoryLine.stock_item_id)
        .outerjoin(party_l, party_l.id == Voucher.party_ledger_id)
        .where(Voucher.company_id == company_id, Voucher.status == POSTED, Voucher.voucher_type_id.in_(list(chosen) or [None]),
               Voucher.voucher_date.between(from_date, to_date))
        .order_by(Voucher.voucher_date, Voucher.voucher_number, VoucherInventoryLine.line_no)).all()
    groups = _group_names(db, company_id)
    rm = RawMaterialCost(production_costs(db, company_id, to_date)) if cost == "rm" else None
    fifo = fifo_line_costs(db, company_id, to_date, {line.stock_item_id for line, *_ in rows}) if cost == "fifo" else None
    costed = rm is not None or fifo is not None

    agg: dict = {}
    lines_out = []
    total = {"quantity": ZERO, "value": ZERO, "cost": ZERO, "vouchers": set(), "no_cost": 0}
    for line, v, base, vtype, it, party_name in rows:
        if party and v.party_ledger_id != party or item and it.id != item or category and (it.category or "") != category \
                or stock_group and it.group_id != stock_group:
            continue
        sign = Decimal(-1) if base == ret else Decimal(1)
        qty, value = abs(line.quantity) * sign, line.amount * sign
        if rm is not None:
            unit_cost = rm.per_unit(it.id, v.voucher_date)
            line_cost = unit_cost * abs(line.quantity) * sign if unit_cost is not None else None
        elif fifo is not None:
            raw = fifo.get(line.id)
            line_cost = raw * sign if raw is not None else None
            unit_cost = raw / abs(line.quantity) if raw is not None and line.quantity else None
        else:
            unit_cost = line_cost = None
        key = {"party": (str(v.party_ledger_id), party_name or "(no party)"),
               "date": (str(v.voucher_date), str(v.voucher_date)),
               "month": (v.voucher_date.strftime("%Y-%m"), v.voucher_date.strftime("%b %Y")),
               "item": (str(it.id), it.name),
               "category": (it.category or "", it.category or "(no category)"),
               "stock_group": (str(it.group_id), groups.get(it.group_id, "(no group)")),
               "voucher": (str(v.id), f"{vtype} {v.voucher_number}")}[by]
        row = agg.setdefault(key[0], {"key": key[0], "name": key[1], "quantity": ZERO, "value": ZERO, "cost": ZERO,
                                       "vouchers": set(), "no_cost": 0, "units": set(), "date": str(v.voucher_date),
                                       "party": party_name, "voucher_type": vtype, "voucher_number": v.voucher_number})
        for target in (row, total):
            target["quantity"] += qty
            target["value"] += value
            target["vouchers"].add(v.id)
            if costed:
                if line_cost is None:
                    target["no_cost"] += 1
                else:
                    target["cost"] += line_cost
        if line.unit:
            row["units"].add(line.unit)
        if by == "voucher":
            lines_out.append({"voucher_id": str(v.id), "date": str(v.voucher_date), "voucher_type": vtype,
                              "voucher_number": v.voucher_number, "party": party_name, "item": it.name,
                              "quantity": _qty(qty), "unit": line.unit, "rate": m(line.rate),
                              "value": m(value), **({"cost_per_unit": m(unit_cost) if unit_cost is not None else None,
                                                     "cost": m(line_cost) if line_cost is not None else None,
                                                     "margin": m(value - line_cost) if line_cost is not None else None,
                                                     "margin_pct": m((value - line_cost) / value * 100)
                                                     if line_cost is not None and value else None}
                                                    if costed else {})})

    def out(r):
        res = {"key": r["key"], "name": r["name"], "vouchers": len(r["vouchers"]), "value": m(r["value"]),
               "quantity": _qty(r["quantity"]) if len(r.get("units", ())) <= 1 else None,
               "unit": next(iter(r["units"])) if len(r.get("units", ())) == 1 else None}
        if costed:
            res.update(cost=m(r["cost"]), margin=m(r["value"] - r["cost"]),
                       margin_pct=m(((r["value"] - r["cost"]) / r["value"] * 100) if r["value"] else ZERO),
                       lines_without_cost=r["no_cost"])
        return res

    order = {"date": lambda r: r["key"], "month": lambda r: r["key"], "voucher": lambda r: (r["date"], r["name"])}
    rows_out = sorted(agg.values(), key=order.get(by, lambda r: -abs(r["value"])))
    result = {
        "side": side, "by": by, "from": str(from_date), "to": str(to_date), "include_returns": include_returns,
        "cost": cost,
        "voucher_types": [{"name": t.name, "base_type": t.base_type, "counted": t.id in chosen} for t in types],
        "rows": [out(r) for r in rows_out],
        "total": {"vouchers": len(total["vouchers"]), "value": m(total["value"]),
                  **({"cost": m(total["cost"]), "margin": m(total["value"] - total["cost"]),
                      "margin_pct": m(((total["value"] - total["cost"]) / total["value"] * 100) if total["value"] else ZERO),
                      "lines_without_cost": total["no_cost"]} if costed else {})},
    }
    if by == "voucher":
        result["lines"] = lines_out
    return result


# ------------------------------------------------------------------ FIFO costing

def _fifo_moves(db: Session, company_id: uuid.UUID, to_date: date, item_ids) -> dict[uuid.UUID, list]:
    """Stock movements per item up to `to_date`: [(date, line, base type)]. Memo vouchers move no stock."""
    rows = db.execute(
        select(VoucherInventoryLine, Voucher.voucher_date, VoucherType.base_type, VoucherType.name)
        .join(Voucher, Voucher.id == VoucherInventoryLine.voucher_id).join(VoucherType)
        .where(Voucher.company_id == company_id, Voucher.status == POSTED, VoucherType.affects_stock.is_(True),
               Voucher.voucher_date <= to_date, VoucherInventoryLine.stock_item_id.in_(list(item_ids) or [None]))).all()
    moves: dict[uuid.UUID, list] = defaultdict(list)
    for line, d, base, vtype in rows:
        if not MEMO_TYPE.search(vtype):
            moves[line.stock_item_id].append((d, line, base))
    return moves


def _run_fifo(it: StockItem, moves: list, from_date: date):
    """Walks one item's movements first-in-first-out.

    Returns (opening (qty, value) at from_date, remaining layers, period totals, cost per line id). An issue
    takes the cost of the oldest stock on hand; beyond that (negative stock) the last known rate. A sales
    return comes back at the cost it last went out at, not at its selling price."""
    layers: list[list] = []                    # [quantity, value] oldest first
    if it.opening_qty:
        layers.append([Decimal(it.opening_qty), Decimal(it.opening_value or 0)])
    last_rate = (Decimal(it.opening_value or 0) / Decimal(it.opening_qty)) if it.opening_qty else ZERO
    issue_rate = None                          # cost per unit of the latest issue
    acc = defaultdict(lambda: ZERO)
    line_cost: dict[uuid.UUID, Decimal] = {}
    snap = None
    # Same day: receipts before issues, so a purchase and its sale on one date do not go negative.
    for d, line, base in sorted(moves, key=lambda x: (x[0], not _inward(x[1]))):
        if snap is None and d >= from_date:
            snap = (sum((q for q, _ in layers), ZERO), sum((v for _, v in layers), ZERO))
        in_period = d >= from_date
        qty, value = abs(line.quantity), line.amount
        if _inward(line):
            if base == "Credit Note" and qty:       # goods returned by a customer
                value = qty * (issue_rate if issue_rate is not None else last_rate)
                line_cost[line.id] = value
                if in_period:
                    acc["returned_value"] += line.amount
                    acc["returned_cost"] += value
            if qty:
                layers.append([qty, value])
                if base != "Credit Note":
                    last_rate = value / qty
            elif layers:
                layers[-1][1] += value          # value-only receipt (e.g. freight added to the last lot)
            if in_period:
                acc["in_qty"] += qty
                acc["in_value"] += value
            continue
        cost, need = ZERO, qty
        while need > 0 and layers:
            lq, lv = layers[0]
            take = min(lq, need)
            part = lv * take / lq if lq else ZERO
            cost += part
            layers[0] = [lq - take, lv - part]
            need -= take
            if layers[0][0] <= 0:
                layers.pop(0)
        if need > 0:                            # issued more than on hand
            cost += need * last_rate
            if in_period:
                acc["short_qty"] += need
        if qty == 0 and value:                  # value-only issue
            cost = value
            if layers:
                layers[-1][1] -= value
        if qty:
            issue_rate = cost / qty
        line_cost[line.id] = cost
        if in_period:
            acc["out_qty"] += qty
            acc["out_cost"] += cost
            if base == "Sales":                 # gross profit is about sales, not issues to production
                acc["sold_qty"] += qty
                acc["sold_value"] += value
                acc["sold_cost"] += cost
    if snap is None:
        snap = (sum((q for q, _ in layers), ZERO), sum((v for _, v in layers), ZERO))
    return snap, layers, acc, line_cost


def fifo_line_costs(db: Session, company_id: uuid.UUID, to_date: date, item_ids) -> dict[uuid.UUID, Decimal]:
    """FIFO cost of each stock line (issues, and sales returns at the cost they went out at)."""
    items = {i.id: i for i in db.scalars(select(StockItem).where(StockItem.company_id == company_id,
                                                               StockItem.id.in_(list(item_ids) or [None])))}
    moves = _fifo_moves(db, company_id, to_date, items)
    costs: dict[uuid.UUID, Decimal] = {}
    for iid, it in items.items():
        costs.update(_run_fifo(it, moves.get(iid, []), date.min)[3])
    return costs


def fifo_costing(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, *, item: uuid.UUID | None = None,
                 category: str | None = None, stock_group: uuid.UUID | None = None, only_moved: bool = True) -> dict:
    """Stock valued first-in-first-out: each issue takes the cost of the oldest stock still on hand.

    Per item: opening, inward, outward (with its FIFO cost) and closing, for the period. Stock issued
    beyond what is on hand (negative stock) is costed at the last known rate and counted in `short_qty`.
    Gross profit is on sales less returns."""
    items = {i.id: i for i in db.scalars(select(StockItem).where(StockItem.company_id == company_id))
             if (not item or i.id == item) and (not category or (i.category or "") == category)
             and (not stock_group or i.group_id == stock_group)}
    moves = _fifo_moves(db, company_id, to_date, items)
    groups = _group_names(db, company_id)
    out_rows = []
    totals = defaultdict(lambda: ZERO)
    for iid, it in items.items():
        snap, layers, acc, _ = _run_fifo(it, moves.get(iid, []), from_date)
        close_q, close_v = sum((q for q, _ in layers), ZERO), sum((v for _, v in layers), ZERO)
        if only_moved and not acc and not snap[0] and not close_q:
            continue
        sold_value = acc["sold_value"] - acc["returned_value"]
        sold_cost = acc["sold_cost"] - acc["returned_cost"]
        row = {"item_id": str(iid), "item": it.name, "unit": it.unit, "category": it.category,
               "stock_group": groups.get(it.group_id), "opening_qty": _qty(snap[0]), "opening_value": m(snap[1]),
               "in_qty": _qty(acc["in_qty"]), "in_value": m(acc["in_value"]),
               "out_qty": _qty(acc["out_qty"]), "out_cost": m(acc["out_cost"]),
               "sold_qty": _qty(acc["sold_qty"]), "sold_value": m(sold_value),
               "sold_cost": m(sold_cost), "gross_profit": m(sold_value - sold_cost),
               "closing_qty": _qty(close_q), "closing_value": m(close_v),
               "closing_rate": m(close_v / close_q) if close_q else None,
               "short_qty": _qty(acc["short_qty"]) if acc["short_qty"] else None}
        out_rows.append(row)
        for k, v in (("opening_value", snap[1]), ("in_value", acc["in_value"]), ("out_cost", acc["out_cost"]),
                     ("sold_value", sold_value), ("sold_cost", sold_cost), ("closing_value", close_v)):
            totals[k] += v
    out_rows.sort(key=lambda r: r["item"].lower())
    return {"from": str(from_date), "to": str(to_date), "method": "FIFO", "items": out_rows,
            "total": {k: m(v) for k, v in totals.items()} | {"gross_profit": m(totals["sold_value"] - totals["sold_cost"])}}
