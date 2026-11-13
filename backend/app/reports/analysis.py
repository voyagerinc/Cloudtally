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
                   raw_material_cost: bool = False) -> dict:
    """Item lines of sales (or purchases) in the period, summed by party, date, month, item, category or
    stock group, or listed per voucher. Returns (credit/debit notes) count negative."""
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
    rm = RawMaterialCost(production_costs(db, company_id, to_date)) if raw_material_cost else None

    agg: dict = {}
    lines_out = []
    total = {"quantity": ZERO, "value": ZERO, "rm_cost": ZERO, "vouchers": set(), "no_rm": 0}
    for line, v, base, vtype, it, party_name in rows:
        if party and v.party_ledger_id != party or item and it.id != item or category and (it.category or "") != category \
                or stock_group and it.group_id != stock_group:
            continue
        sign = Decimal(-1) if base == ret else Decimal(1)
        qty, value = abs(line.quantity) * sign, line.amount * sign
        unit_rm = rm.per_unit(it.id, v.voucher_date) if rm else None
        rm_cost = (unit_rm * abs(line.quantity) * sign) if unit_rm is not None else None
        key = {"party": (str(v.party_ledger_id), party_name or "(no party)"),
               "date": (str(v.voucher_date), str(v.voucher_date)),
               "month": (v.voucher_date.strftime("%Y-%m"), v.voucher_date.strftime("%b %Y")),
               "item": (str(it.id), it.name),
               "category": (it.category or "", it.category or "(no category)"),
               "stock_group": (str(it.group_id), groups.get(it.group_id, "(no group)")),
               "voucher": (str(v.id), f"{vtype} {v.voucher_number}")}[by]
        row = agg.setdefault(key[0], {"key": key[0], "name": key[1], "quantity": ZERO, "value": ZERO, "rm_cost": ZERO,
                                       "vouchers": set(), "no_rm": 0, "units": set(), "date": str(v.voucher_date),
                                       "party": party_name, "voucher_type": vtype, "voucher_number": v.voucher_number})
        for target in (row, total):
            target["quantity"] += qty
            target["value"] += value
            target["vouchers"].add(v.id)
            if rm:
                if rm_cost is None:
                    target["no_rm"] += 1
                else:
                    target["rm_cost"] += rm_cost
        if line.unit:
            row["units"].add(line.unit)
        if by == "voucher":
            lines_out.append({"voucher_id": str(v.id), "date": str(v.voucher_date), "voucher_type": vtype,
                              "voucher_number": v.voucher_number, "party": party_name, "item": it.name,
                              "quantity": _qty(qty), "unit": line.unit, "rate": m(line.rate),
                              "value": m(value), **({"rm_cost_per_unit": m(unit_rm) if unit_rm is not None else None,
                                                     "rm_cost": m(rm_cost) if rm_cost is not None else None,
                                                     "margin": m(value - rm_cost) if rm_cost is not None else None}
                                                    if rm else {})})

    def out(r):
        res = {"key": r["key"], "name": r["name"], "vouchers": len(r["vouchers"]), "value": m(r["value"]),
               "quantity": _qty(r["quantity"]) if len(r.get("units", ())) <= 1 else None,
               "unit": next(iter(r["units"])) if len(r.get("units", ())) == 1 else None}
        if rm:
            res.update(rm_cost=m(r["rm_cost"]), margin=m(r["value"] - r["rm_cost"]),
                       margin_pct=m(((r["value"] - r["rm_cost"]) / r["value"] * 100) if r["value"] else ZERO),
                       lines_without_rm_cost=r["no_rm"])
        return res

    order = {"date": lambda r: r["key"], "month": lambda r: r["key"], "voucher": lambda r: (r["date"], r["name"])}
    rows_out = sorted(agg.values(), key=order.get(by, lambda r: -abs(r["value"])))
    result = {
        "side": side, "by": by, "from": str(from_date), "to": str(to_date), "include_returns": include_returns,
        "voucher_types": [{"name": t.name, "base_type": t.base_type, "counted": t.id in chosen} for t in types],
        "rows": [out(r) for r in rows_out],
        "total": {"vouchers": len(total["vouchers"]), "value": m(total["value"]),
                  **({"rm_cost": m(total["rm_cost"]), "margin": m(total["value"] - total["rm_cost"]),
                      "margin_pct": m(((total["value"] - total["rm_cost"]) / total["value"] * 100) if total["value"] else ZERO),
                      "lines_without_rm_cost": total["no_rm"]} if rm else {})},
    }
    if by == "voucher":
        result["lines"] = lines_out
    return result


# ------------------------------------------------------------------ FIFO costing

def fifo_costing(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, *, item: uuid.UUID | None = None,
                 category: str | None = None, stock_group: uuid.UUID | None = None, only_moved: bool = True) -> dict:
    """Stock valued first-in-first-out: each issue takes the cost of the oldest stock still on hand.

    Per item: opening, inward, outward (with its FIFO cost) and closing, for the period. Stock issued
    beyond what is on hand (negative stock) is costed at the last known rate and counted in `short_qty`."""
    items = {i.id: i for i in db.scalars(select(StockItem).where(StockItem.company_id == company_id))
             if (not item or i.id == item) and (not category or (i.category or "") == category)
             and (not stock_group or i.group_id == stock_group)}
    rows = db.execute(
        select(VoucherInventoryLine, Voucher.voucher_date, VoucherType.base_type, VoucherType.name)
        .join(Voucher, Voucher.id == VoucherInventoryLine.voucher_id).join(VoucherType)
        .where(Voucher.company_id == company_id, Voucher.status == POSTED, VoucherType.affects_stock.is_(True),
               Voucher.voucher_date <= to_date, VoucherInventoryLine.stock_item_id.in_(list(items) or [None]))).all()
    moves: dict[uuid.UUID, list] = defaultdict(list)
    for line, d, base, vtype in rows:
        if not MEMO_TYPE.search(vtype):          # a proforma or quotation moves no stock
            moves[line.stock_item_id].append((d, line, base))
    groups = _group_names(db, company_id)
    out_rows = []
    totals = defaultdict(lambda: ZERO)
    for iid, it in items.items():
        layers: list[list] = []                    # [quantity, value] oldest first
        if it.opening_qty:
            layers.append([Decimal(it.opening_qty), Decimal(it.opening_value or 0)])
        last_rate = (Decimal(it.opening_value or 0) / Decimal(it.opening_qty)) if it.opening_qty else ZERO
        acc = defaultdict(lambda: ZERO)
        snap = None
        # Same day: receipts before issues, so a purchase and its sale on one date do not go negative.
        for d, line, base in sorted(moves.get(iid, []), key=lambda x: (x[0], not _inward(x[1]))):
            if snap is None and d >= from_date:
                snap = (sum((q for q, _ in layers), ZERO), sum((v for _, v in layers), ZERO))
            in_period = d >= from_date
            qty, value = abs(line.quantity), line.amount
            if _inward(line):
                if qty:
                    layers.append([qty, value])
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
            if in_period:
                acc["out_qty"] += qty
                acc["out_cost"] += cost
                if base == "Sales":                 # gross profit is about sales, not issues to production
                    acc["sold_qty"] += qty
                    acc["sold_value"] += value
                    acc["sold_cost"] += cost
        if snap is None:
            snap = (sum((q for q, _ in layers), ZERO), sum((v for _, v in layers), ZERO))
        close_q, close_v = sum((q for q, _ in layers), ZERO), sum((v for _, v in layers), ZERO)
        if only_moved and not acc and not snap[0] and not close_q:
            continue
        row = {"item_id": str(iid), "item": it.name, "unit": it.unit, "category": it.category,
               "stock_group": groups.get(it.group_id), "opening_qty": _qty(snap[0]), "opening_value": m(snap[1]),
               "in_qty": _qty(acc["in_qty"]), "in_value": m(acc["in_value"]),
               "out_qty": _qty(acc["out_qty"]), "out_cost": m(acc["out_cost"]),
               "sold_qty": _qty(acc["sold_qty"]), "sold_value": m(acc["sold_value"]),
               "sold_cost": m(acc["sold_cost"]), "gross_profit": m(acc["sold_value"] - acc["sold_cost"]),
               "closing_qty": _qty(close_q), "closing_value": m(close_v),
               "closing_rate": m(close_v / close_q) if close_q else None,
               "short_qty": _qty(acc["short_qty"]) if acc["short_qty"] else None}
        out_rows.append(row)
        for k, v in (("opening_value", snap[1]), ("in_value", acc["in_value"]), ("out_cost", acc["out_cost"]),
                     ("sold_value", acc["sold_value"]), ("sold_cost", acc["sold_cost"]), ("closing_value", close_v)):
            totals[k] += v
    out_rows.sort(key=lambda r: r["item"].lower())
    return {"from": str(from_date), "to": str(to_date), "method": "FIFO", "items": out_rows,
            "total": {k: m(v) for k, v in totals.items()} | {"gross_profit": m(totals["sold_value"] - totals["sold_cost"])}}
