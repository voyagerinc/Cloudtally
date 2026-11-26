"""Sales and purchase analysis, FIFO costing, and raw-material cost of sales from production vouchers."""
import bisect
import re
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.models import Ledger, StockGroup, StockItem, Voucher, VoucherEntry, VoucherInventoryLine, VoucherType
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


def _main_groups(db: Session, company_id) -> dict:
    """Each stock group's main group: the one straight under Primary that it sits in (itself when it is one)."""
    groups = {g.id: g for g in db.scalars(select(StockGroup).where(StockGroup.company_id == company_id))}
    out = {}
    for gid in groups:
        top, seen = gid, set()
        while groups[top].parent_id in groups and top not in seen:
            seen.add(top)
            top = groups[top].parent_id
        out[gid] = groups[top].name
    return out


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

ROUND_OFF = re.compile(r"round", re.I)


def line_taxes(db: Session, company_id: uuid.UUID, rows) -> dict:
    """GST of each item line. A voucher's tax ledgers are shared over what is taxed in it: its item lines
    and other income/charge ledgers such as packing & forwarding, each weighted by its GST rate (charges
    take the highest item rate). A line gets only its own share: the tax on charges belongs to no item, so
    the lines add up to the voucher's tax only when it has no such charges."""
    from app.reports import _groups, _is_tax_ledger
    by_voucher: dict = defaultdict(list)
    for line, v, _base, _vtype, it, _party in rows:
        by_voucher[v.id].append((line, it, v.party_ledger_id))
    if not by_voucher:
        return {}
    groups = _groups(db, company_id)
    ledgers = {l.id: l for l in db.scalars(select(Ledger).where(Ledger.company_id == company_id))}
    tax_ids = {i for i, l in ledgers.items() if _is_tax_ledger(l, groups)}
    round_ids = {i for i, l in ledgers.items() if ROUND_OFF.search(l.name)}
    tax, other = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    ids = list(by_voucher)
    for n in range(0, len(ids), 5000):
        for vid, lid, amount in db.execute(select(VoucherEntry.voucher_id, VoucherEntry.ledger_id, VoucherEntry.amount)
                                           .where(VoucherEntry.voucher_id.in_(ids[n:n + 5000]))):
            if lid in tax_ids:
                tax[vid] += abs(amount)
            elif lid not in round_ids and lid != by_voucher[vid][0][2]:
                other[vid] += abs(amount)   # the item ledgers (= the item lines) plus charges
    out = {}
    for vid, lines in by_voucher.items():
        if not tax[vid]:
            out.update({line.id: ZERO for line, *_ in lines})
            continue
        items_value = sum((abs(line.amount) for line, *_ in lines), ZERO)
        rates = [Decimal(str(it.gst_rate)) if it.gst_rate else None for _, it, _ in lines]
        top = max((r for r in rates if r), default=Decimal(1))
        weights = [abs(line.amount) * (r or top) for (line, *_), r in zip(lines, rates)]
        charges = max(other[vid] - items_value, ZERO)
        total_w = sum(weights, ZERO) + charges * top
        for (line, *_), w in zip(lines, weights):
            out[line.id] = (tax[vid] * w / total_w) if total_w else ZERO
    return out

def trade_analysis(db: Session, company_id: uuid.UUID, side: str, from_date: date, to_date: date, *,
                   by: str = "party", party: uuid.UUID | None = None, item: uuid.UUID | None = None,
                   category: str | None = None, stock_group: uuid.UUID | None = None,
                   voucher_types: list[str] | None = None, include_returns: bool = True,
                   cost: str | None = None, alt: bool = False) -> dict:
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
    taxes = line_taxes(db, company_id, rows)

    agg: dict = {}
    lines_out = []
    total = {"quantity": ZERO, "value": ZERO, "tax": ZERO, "cost": ZERO, "vouchers": set(), "no_cost": 0}

    def alt_of(line, it):
        """The line's quantity in the alternate unit: as Tally stored it, else by the item's conversion."""
        if line.alt_unit and line.alt_quantity is not None:
            return abs(line.alt_quantity), line.alt_unit
        if it.alternate_unit and it.conversion and it.denominator:
            return abs(line.quantity) * Decimal(it.conversion) / Decimal(it.denominator), it.alternate_unit
        return None, None
    for line, v, base, vtype, it, party_name in rows:
        if party and v.party_ledger_id != party or item and it.id != item or category and (it.category or "") != category \
                or stock_group and it.group_id != stock_group:
            continue
        sign = Decimal(-1) if base == ret else Decimal(1)
        qty, value = abs(line.quantity) * sign, line.amount * sign
        tax = taxes.get(line.id, ZERO) * sign
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
        row = agg.setdefault(key[0], {"key": key[0], "name": key[1], "quantity": ZERO, "value": ZERO, "tax": ZERO,
                                       "cost": ZERO, "vouchers": set(), "no_cost": 0, "units": set(), "date": str(v.voucher_date),
                                       "party": party_name, "voucher_type": vtype, "voucher_number": v.voucher_number,
                                       "alt_quantity": ZERO, "alt_units": set()})
        if alt:
            aq, au = alt_of(line, it)
            row["alt_units"].add(au)
            if aq is not None:
                row["alt_quantity"] += aq * sign
        for target in (row, total):
            target["quantity"] += qty
            target["value"] += value
            target["tax"] += tax
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
                              "item_group": groups.get(it.group_id, "(no group)") if it.group_id else None,
                              "category": it.category,
                              "quantity": _qty(qty), "unit": line.unit, "rate": m(line.rate),
                              "value": m(value), "tax": m(tax), **({"cost_per_unit": m(unit_cost) if unit_cost is not None else None,
                                                     "cost": m(line_cost) if line_cost is not None else None,
                                                     "margin": m(value - line_cost) if line_cost is not None else None,
                                                     "margin_pct": m((value - line_cost) / value * 100)
                                                     if line_cost is not None and value else None}
                                                    if costed else {})})

    def out(r):
        res = {"key": r["key"], "name": r["name"], "vouchers": len(r["vouchers"]), "value": m(r["value"]),
               "tax": m(r["tax"]),
               "quantity": _qty(r["quantity"]) if len(r.get("units", ())) <= 1 else None,
               "unit": next(iter(r["units"])) if len(r.get("units", ())) == 1 else None}
        if costed:
            res.update(cost=m(r["cost"]), margin=m(r["value"] - r["cost"]),
                       margin_pct=m(((r["value"] - r["cost"]) / r["value"] * 100) if r["value"] else ZERO),
                       lines_without_cost=r["no_cost"])
        if alt:     # one alternate unit on every line: its quantity; else none (e.g. no alternate unit)
            one = len(r["alt_units"]) == 1 and None not in r["alt_units"]
            res.update(alt_quantity=_qty(r["alt_quantity"]) if one else None,
                       alt_unit=next(iter(r["alt_units"])) if one else None)
        return res

    order = {"date": lambda r: r["key"], "month": lambda r: r["key"], "voucher": lambda r: (r["date"], r["name"])}
    rows_out = sorted(agg.values(), key=order.get(by, lambda r: -abs(r["value"])))
    result = {
        "side": side, "by": by, "from": str(from_date), "to": str(to_date), "include_returns": include_returns,
        "cost": cost,
        "voucher_types": [{"name": t.name, "base_type": t.base_type, "counted": t.id in chosen} for t in types],
        "rows": [out(r) for r in rows_out],
        "total": {"vouchers": len(total["vouchers"]), "value": m(total["value"]), "tax": m(total["tax"]),
                  **({"cost": m(total["cost"]), "margin": m(total["value"] - total["cost"]),
                      "margin_pct": m(((total["value"] - total["cost"]) / total["value"] * 100) if total["value"] else ZERO),
                      "lines_without_cost": total["no_cost"]} if costed else {})},
    }
    if by == "voucher":
        result["lines"] = lines_out
    return result


# ------------------------------------------------------------------ FIFO costing

def _fifo_moves(db: Session, company_id: uuid.UUID, to_date: date, item_ids,
                exclude_voucher: uuid.UUID | None = None) -> dict[uuid.UUID, list]:
    """Stock movements per item up to `to_date`: [(date, line, base type)]. Memo vouchers move no stock."""
    stmt = (select(VoucherInventoryLine, Voucher.voucher_date, VoucherType.base_type, VoucherType.name)
            .join(Voucher, Voucher.id == VoucherInventoryLine.voucher_id).join(VoucherType)
            .where(Voucher.company_id == company_id, Voucher.status == POSTED, VoucherType.affects_stock.is_(True),
                   Voucher.voucher_date <= to_date, VoucherInventoryLine.stock_item_id.in_(list(item_ids) or [None])))
    if exclude_voucher:
        stmt = stmt.where(Voucher.id != exclude_voucher)
    rows = db.execute(stmt).all()
    moves: dict[uuid.UUID, list] = defaultdict(list)
    for line, d, base, vtype in rows:
        if not MEMO_TYPE.search(vtype):
            moves[line.stock_item_id].append((d, line, base))
    return moves


def _run_fifo(it: StockItem, moves: list, from_date: date, monthly: dict | None = None):
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
    on_hand = lambda: (sum((q for q, _ in layers), ZERO), sum((v for _, v in layers), ZERO))  # noqa: E731
    if monthly is not None:
        monthly.update(snaps=[], acc=defaultdict(lambda: defaultdict(lambda: ZERO)), i=0)
    month_acc = None

    def add(key, value):
        acc[key] += value
        if month_acc is not None:
            month_acc[key] += value
    # Same day: receipts before issues, so a purchase and its sale on one date do not go negative.
    for d, line, base in sorted(moves, key=lambda x: (x[0], not _inward(x[1]))):
        if snap is None and d >= from_date:
            snap = (sum((q for q, _ in layers), ZERO), sum((v for _, v in layers), ZERO))
        in_period = d >= from_date
        if monthly is not None:
            marks = monthly["marks"]
            while monthly["i"] < len(marks) and d >= marks[monthly["i"]]:
                monthly["snaps"].append(on_hand())
                monthly["i"] += 1
            month_acc = monthly["acc"][d.strftime("%Y-%m")] if in_period else None
        qty, value = abs(line.quantity), line.amount
        if _inward(line):
            if base == "Credit Note" and qty:       # goods returned by a customer
                value = qty * (issue_rate if issue_rate is not None else last_rate)
                line_cost[line.id] = value
                if in_period:
                    add("returned_value", line.amount)
                    add("returned_cost", value)
            if qty:
                layers.append([qty, value])
                if base != "Credit Note":
                    last_rate = value / qty
            elif layers:
                layers[-1][1] += value          # value-only receipt (e.g. freight added to the last lot)
            if in_period:
                add("in_qty", qty)
                add("in_value", value)
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
                add("short_qty", need)
        if qty == 0 and value:                  # value-only issue
            cost = value
            if layers:
                layers[-1][1] -= value
        if qty:
            issue_rate = cost / qty
        line_cost[line.id] = cost
        if in_period:
            add("out_qty", qty)
            add("out_cost", cost)
            if base == "Sales":                 # gross profit is about sales, not issues to production
                add("sold_qty", qty)
                add("sold_value", value)
                add("sold_cost", cost)
    if snap is None:
        snap = (sum((q for q, _ in layers), ZERO), sum((v for _, v in layers), ZERO))
    if monthly is not None:
        while len(monthly["snaps"]) < len(monthly["marks"]):
            monthly["snaps"].append(on_hand())
    return snap, layers, acc, line_cost


def fifo_rates(db: Session, company_id: uuid.UUID, as_of: date, names: list[str],
               exclude_voucher: uuid.UUID | None = None) -> dict[str, dict]:
    """Each item's FIFO closing rate on `as_of` (closing value / closing quantity of the stock on hand),
    the rate a voucher entered that day takes by default. With nothing on hand: the last inward rate
    (basis "last receipt"), else the opening rate; None when the item has never had a value."""
    items = {i.id: i for i in db.scalars(select(StockItem).where(StockItem.company_id == company_id,
                                                               StockItem.name.in_(list(names) or [""])))}
    moves = _fifo_moves(db, company_id, as_of, items, exclude_voucher)
    out = {}
    for iid, it in items.items():
        _snap, layers, _acc, _ = _run_fifo(it, moves.get(iid, []), date.min)
        qty, value = sum((q for q, _ in layers), ZERO), sum((v for _, v in layers), ZERO)
        if qty > 0 and value > 0:
            out[it.name] = {"rate": m(value / qty), "qty": _qty(qty), "value": m(value), "basis": "FIFO closing"}
            continue
        receipts = [line for d, line, base in sorted(moves.get(iid, []), key=lambda x: x[0])
                    if _inward(line) and line.quantity and base != "Credit Note" and line.amount]
        if receipts:
            last = receipts[-1]
            out[it.name] = {"rate": m(last.amount / abs(last.quantity)), "qty": _qty(qty), "value": m(value),
                            "basis": "last receipt (no stock on hand)"}
        elif it.opening_qty and it.opening_value:
            out[it.name] = {"rate": m(Decimal(it.opening_value) / Decimal(it.opening_qty)), "qty": _qty(qty),
                            "value": m(value), "basis": "opening rate (no stock on hand)"}
        else:
            out[it.name] = {"rate": None, "qty": _qty(qty), "value": m(value), "basis": "no rate yet"}
    return out


def fifo_line_costs(db: Session, company_id: uuid.UUID, to_date: date, item_ids) -> dict[uuid.UUID, Decimal]:
    """FIFO cost of each stock line (issues, and sales returns at the cost they went out at)."""
    items = {i.id: i for i in db.scalars(select(StockItem).where(StockItem.company_id == company_id,
                                                               StockItem.id.in_(list(item_ids) or [None])))}
    moves = _fifo_moves(db, company_id, to_date, items)
    costs: dict[uuid.UUID, Decimal] = {}
    for iid, it in items.items():
        costs.update(_run_fifo(it, moves.get(iid, []), date.min)[3])
    return costs


def group_wise(db: Session, company_id: uuid.UUID, side: str, from_date: date, to_date: date, *,
               party: uuid.UUID | None = None, category: str | None = None, stock_group: uuid.UUID | None = None,
               voucher_types: list[str] | None = None, include_returns: bool = True, cost: str | None = "fifo",
               units: bool = False) -> dict:
    """Sales (or purchases) by stock group as a tree, like Tally's group-wise reports: each group holds its
    sub-groups and items, and its figures include everything under it. stock_group keeps that group's
    branch only, sub-groups included."""
    data = trade_analysis(db, company_id, side, from_date, to_date, by="item", party=party, category=category,
                          voucher_types=voucher_types, include_returns=include_returns, cost=cost, alt=units)
    groups = {g.id: g for g in db.scalars(select(StockGroup).where(StockGroup.company_id == company_id))}
    item_group = dict(db.execute(select(StockItem.id, StockItem.group_id).where(StockItem.company_id == company_id)).all())
    costed = cost is not None

    def chain(gid):   # the group and its parents, nearest first
        out, seen = [], set()
        while gid in groups and gid not in seen:
            seen.add(gid)
            out.append(gid)
            gid = groups[gid].parent_id
        return out

    def blank(key, name, kind):
        return {"key": key, "name": name, "kind": kind, "quantity": ZERO, "value": ZERO, "tax": ZERO, "cost": ZERO,
                "no_cost": 0, "items": 0, "units": set(), "children": [], "alt_quantity": ZERO, "alt_units": set()}

    nodes = {}
    roots = []
    for r in data["rows"]:
        gid = item_group.get(uuid.UUID(r["key"]))
        path = chain(gid)
        if stock_group and stock_group not in path:
            continue
        leaf = {"key": r["key"], "name": r["name"], "kind": "item", "quantity": r["quantity"], "unit": r["unit"],
                "value": r["value"], "tax": r["tax"], "vouchers": r["vouchers"],
                **({k: r[k] for k in ("cost", "margin", "margin_pct", "lines_without_cost")} if costed else {}),
                **({"alt_quantity": r["alt_quantity"], "alt_unit": r["alt_unit"]} if units else {})}
        parent_list = roots
        # build from the top: the root group first
        for g in reversed(path):
            if g not in nodes:
                nodes[g] = blank(str(g), groups[g].name, "group")
                parent_list.append(nodes[g])
            parent_list = nodes[g]["children"]
        if not path:
            if "none" not in nodes:
                nodes["none"] = blank("", "(no group)", "group")
                roots.append(nodes["none"])
            parent_list = nodes["none"]["children"]
            path = ["none"]
        parent_list.append(leaf)
        for g in path:
            n = nodes[g]
            n["value"] += Decimal(r["value"])
            n["tax"] += Decimal(r["tax"])
            n["items"] += 1
            if r["quantity"] is not None and r["unit"]:
                n["quantity"] += Decimal(r["quantity"])
            n["units"].add(r["unit"] if r["quantity"] is not None else None)
            if units:
                n["alt_units"].add(r["alt_unit"] if r["alt_quantity"] is not None else None)
                if r["alt_quantity"] is not None:
                    n["alt_quantity"] += Decimal(r["alt_quantity"])
            if costed:
                n["cost"] += Decimal(r["cost"])
                n["no_cost"] += r["lines_without_cost"]

    def with_rates(out):
        """Sale rate and FIFO cost rate per main unit and per alternate unit."""
        q = Decimal(out["quantity"]) if out.get("quantity") is not None else ZERO
        aq = Decimal(out["alt_quantity"]) if out.get("alt_quantity") is not None else ZERO
        out.update(rate=_rate(out["value"], q), alt_rate=_rate(out["value"], aq))
        if costed:
            out.update(cost_rate=_rate(out["cost"], q), cost_alt_rate=_rate(out["cost"], aq))
        return out

    def finish(n):
        if n["kind"] == "item":
            return with_rates(n) if units else n
        one_unit = len(n["units"]) == 1 and None not in n["units"]
        out = {"key": n["key"], "name": n["name"], "kind": "group", "items": n["items"],
               "quantity": _qty(n["quantity"]) if one_unit else None, "unit": next(iter(n["units"])) if one_unit else None,
               "value": m(n["value"]), "tax": m(n["tax"])}
        if costed:
            out.update(cost=m(n["cost"]), margin=m(n["value"] - n["cost"]),
                       margin_pct=m((n["value"] - n["cost"]) / n["value"] * 100) if n["value"] else None,
                       lines_without_cost=n["no_cost"])
        if units:
            one_alt = len(n["alt_units"]) == 1 and None not in n["alt_units"]
            out.update(alt_quantity=_qty(n["alt_quantity"]) if one_alt else None,
                       alt_unit=next(iter(n["alt_units"])) if one_alt else None)
            with_rates(out)
        kids = [finish(c) for c in n["children"]]
        # groups first, then items, each by value (largest first) as in Tally's group summary
        out["children"] = sorted(kids, key=lambda c: (c["kind"] != "group", -abs(Decimal(c["value"]))))
        return out

    tree = sorted((finish(n) for n in roots), key=lambda c: -abs(Decimal(c["value"])))
    total_value = sum((Decimal(n["value"]) for n in tree), ZERO)
    total = {"value": m(total_value), "tax": m(sum((Decimal(n["tax"]) for n in tree), ZERO))}
    if costed:
        tc = sum((Decimal(n["cost"]) for n in tree), ZERO)
        total.update(cost=m(tc), margin=m(total_value - tc),
                     margin_pct=m((total_value - tc) / total_value * 100) if total_value else None,
                     lines_without_cost=sum(n["lines_without_cost"] for n in tree))

    rows = []   # flat, for CSV and Excel: one row per group and item, with its level and group path

    cols = (("quantity", "unit", "rate", "alt_quantity", "alt_unit", "alt_rate", "value", "tax",
             "cost_rate", "cost_alt_rate", "cost", "margin", "margin_pct") if units
            else ("quantity", "unit", "value", "tax", "cost", "margin", "margin_pct"))

    def flatten(n, level, path):
        rows.append({"level": level, "type": n["kind"], "group_path": " > ".join(path), "name": n["name"],
                     **{k: n.get(k) for k in cols if k in n}})
        for c in n.get("children", []):
            flatten(c, level + 1, path + [n["name"]])
    for n in tree:
        flatten(n, 1, [])
    return {"side": side, "from": data["from"], "to": data["to"], "include_returns": include_returns, "cost": cost,
            "voucher_types": data["voucher_types"], "tree": tree, "rows": rows, "total": total}


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


def _rate(value: Decimal, qty: Decimal) -> str | None:
    """Value per unit, or None when there is no quantity."""
    return m(Decimal(value) / Decimal(qty)) if qty else None


def fifo_monthly(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, *, item: uuid.UUID | None = None,
                 category: str | None = None, stock_group: uuid.UUID | None = None) -> dict:
    """FIFO stock costing month by month: for each item and month of the period, opening, inward, outward
    (at FIFO cost) and closing, quantity and value. A month's closing is the next month's opening."""
    from app.reports.mis import months_in
    months = months_in(from_date, to_date)
    marks = [max(mo, from_date) for mo in months] + [to_date + timedelta(days=1)]
    keys = [mo.strftime("%Y-%m") for mo in months]
    items = {i.id: i for i in db.scalars(select(StockItem).where(StockItem.company_id == company_id))
             if (not item or i.id == item) and (not category or (i.category or "") == category)
             and (not stock_group or i.group_id == stock_group)}
    moves = _fifo_moves(db, company_id, to_date, items)
    groups = _group_names(db, company_id)
    # the parent group: the group straight under Primary that the item's group sits in (itself when it is one)
    parent = _main_groups(db, company_id)
    fields = ("opening_value", "in_value", "out_cost", "closing_value")
    month_totals = {k: dict.fromkeys(fields, ZERO) for k in keys}
    total = dict.fromkeys(fields, ZERO)
    out_rows, rows = [], []
    for iid, it in items.items():
        mon: dict = {"marks": marks}
        _, _, acc, _ = _run_fifo(it, moves.get(iid, []), from_date, monthly=mon)
        snaps = mon["snaps"]
        if not acc and not snaps[0][0] and not snaps[-1][0] and not snaps[0][1] and not snaps[-1][1]:
            continue
        mrows = []
        for n, k in enumerate(keys):
            a = mon["acc"].get(k, {})
            (oq, ov), (cq, cv) = snaps[n], snaps[n + 1]
            iq, iv = a.get("in_qty", ZERO), a.get("in_value", ZERO)
            uq, uv = a.get("out_qty", ZERO), a.get("out_cost", ZERO)
            r = {"month": k, "label": months[n].strftime("%b %Y"),
                 "opening_qty": _qty(oq), "opening_rate": _rate(ov, oq), "opening_value": m(ov),
                 "in_qty": _qty(iq), "in_rate": _rate(iv, iq), "in_value": m(iv),
                 "out_qty": _qty(uq), "out_rate": _rate(uv, uq), "out_cost": m(uv),
                 "closing_qty": _qty(cq), "closing_rate": _rate(cv, cq), "closing_value": m(cv),
                 "short_qty": _qty(a["short_qty"]) if a.get("short_qty") else None}
            mrows.append(r)
            for f, v in (("opening_value", ov), ("in_value", a.get("in_value", ZERO)),
                         ("out_cost", a.get("out_cost", ZERO)), ("closing_value", cv)):
                month_totals[k][f] += v
            rows.append({"item": it.name, "stock_group": groups.get(it.group_id),
                         "parent_group": parent.get(it.group_id), "unit": it.unit, "month": r["label"],
                         **{f: r[f] for f in ("opening_qty", "opening_rate", "opening_value", "in_qty", "in_rate",
                                              "in_value", "out_qty", "out_rate", "out_cost", "closing_qty",
                                              "closing_rate", "closing_value")}})
        (oq, ov), (cq, cv) = snaps[0], snaps[-1]
        out_rows.append({"item_id": str(iid), "item": it.name, "unit": it.unit, "category": it.category,
                         "stock_group": groups.get(it.group_id), "parent_group": parent.get(it.group_id),
                         "opening_qty": _qty(oq), "opening_rate": _rate(ov, oq), "opening_value": m(ov),
                         "in_qty": _qty(acc["in_qty"]), "in_rate": _rate(acc["in_value"], acc["in_qty"]),
                         "in_value": m(acc["in_value"]),
                         "out_qty": _qty(acc["out_qty"]), "out_rate": _rate(acc["out_cost"], acc["out_qty"]),
                         "out_cost": m(acc["out_cost"]),
                         "closing_qty": _qty(cq), "closing_rate": _rate(cv, cq), "closing_value": m(cv),
                         "short_qty": _qty(acc["short_qty"]) if acc["short_qty"] else None, "months": mrows})
        for f, v in (("opening_value", ov), ("in_value", acc["in_value"]), ("out_cost", acc["out_cost"]),
                     ("closing_value", cv)):
            total[f] += v
    out_rows.sort(key=lambda r: r["item"].lower())
    rows.sort(key=lambda r: (r["item"].lower(), r["month"]))
    return {"from": str(from_date), "to": str(to_date), "method": "FIFO",
            "months": [{"key": k, "label": mo.strftime("%b %Y")} for k, mo in zip(keys, months)],
            "items": out_rows, "rows": rows, "total": {k: m(v) for k, v in total.items()},
            "month_totals": [{"month": k, "label": mo.strftime("%b %Y"), **{f: m(v) for f, v in month_totals[k].items()}}
                             for k, mo in zip(keys, months)]}


MAIN_LOCATION = "Main Location"     # Tally's default godown


def _main_godowns(db: Session, company_id) -> dict:
    """Each godown's group: the godown straight under Primary that it sits in (itself when it is one)."""
    from app.models import Godown
    gs = {g.id: g for g in db.scalars(select(Godown).where(Godown.company_id == company_id))}
    out = {}
    for g in gs.values():
        top, seen = g, set()
        while top.parent_id in gs and top.id not in seen:
            seen.add(top.id)
            top = gs[top.parent_id]
        out[g.name] = top.name
    return out


def _godown_parents(db: Session, company_id) -> dict:
    """Each godown's own parent (its store group), by name; itself when it is straight under Primary."""
    from app.models import Godown
    gs = {g.id: g for g in db.scalars(select(Godown).where(Godown.company_id == company_id))}
    return {g.name: (gs[g.parent_id].name if g.parent_id in gs else g.name) for g in gs.values()}


def godown_summary(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, *, item: uuid.UUID | None = None,
                   category: str | None = None, stock_group: uuid.UUID | None = None) -> dict:
    """Stock by store (godown) and item for the period: opening, inward, outward and closing, quantity, rate
    and value. Quantities are those of each godown, as Tally keeps them (they may go below zero); outward is
    at FIFO cost (of the item as a whole), inward at its value; opening and closing are valued at the item's
    FIFO rate on those days. Where an item went below zero, its quantities here are Tally's, while the FIFO
    report cannot hold less than nothing."""
    items = {i.id: i for i in db.scalars(select(StockItem).where(StockItem.company_id == company_id))
             if (not item or i.id == item) and (not category or (i.category or "") == category)
             and (not stock_group or i.group_id == stock_group)}
    moves = _fifo_moves(db, company_id, to_date, items)
    groups, main_groups = _group_names(db, company_id), _main_groups(db, company_id)
    store_group = _main_godowns(db, company_id)         # store group: the godown straight under Primary
    store_parent = _godown_parents(db, company_id)      # parent store group: the godown's own parent
    rows = []
    for iid, it in items.items():
        mv = moves.get(iid, [])
        snap, layers, acc, line_cost = _run_fifo(it, mv, from_date)
        close_q, close_v = sum((q for q, _ in layers), ZERO), sum((v for _, v in layers), ZERO)
        if not mv and not snap[0] and not close_q:
            continue
        open_rate = snap[1] / snap[0] if snap[0] else None
        close_rate = close_v / close_q if close_q else None
        by = defaultdict(lambda: {"oq": ZERO, "iq": ZERO, "iv": ZERO, "uq": ZERO, "uv": ZERO})
        allocs = it.opening_allocations or []
        for a in allocs:
            by[a.get("godown") or MAIN_LOCATION]["oq"] += Decimal(str(a.get("qty") or 0))
        if not allocs and it.opening_qty:
            by[MAIN_LOCATION]["oq"] += Decimal(it.opening_qty)
        for d, line, base in mv:
            g = by[line.godown or MAIN_LOCATION]
            qty = abs(line.quantity)
            inward = _inward(line)
            if d < from_date:
                g["oq"] += qty if inward else -qty
            elif inward:
                g["iq"] += qty
                g["iv"] += line_cost.get(line.id, line.amount) if base == "Credit Note" else line.amount
            else:
                g["uq"] += qty
                g["uv"] += line_cost.get(line.id, ZERO)
        for name, g in by.items():
            cq = g["oq"] + g["iq"] - g["uq"]
            if not any((g["oq"], g["iq"], g["uq"], cq, g["iv"], g["uv"])):     # keep value-only lines (freight ...)
                continue
            ov = g["oq"] * open_rate if open_rate is not None else ZERO
            cv = cq * close_rate if close_rate is not None else ZERO
            rows.append({"store": name, "store_group": store_group.get(name, name),
                         "store_parent": store_parent.get(name, name), "item_id": str(iid),
                         "item": it.name, "stock_group": groups.get(it.group_id),
                         "parent_group": _main_groups_name(main_groups, it.group_id), "unit": it.unit,
                         "opening_qty": _qty(g["oq"]), "opening_rate": _rate(ov, g["oq"]), "opening_value": m(ov),
                         "in_qty": _qty(g["iq"]), "in_rate": _rate(g["iv"], g["iq"]), "in_value": m(g["iv"]),
                         "out_qty": _qty(g["uq"]), "out_rate": _rate(g["uv"], g["uq"]), "out_cost": m(g["uv"]),
                         "closing_qty": _qty(cq), "closing_rate": _rate(cv, cq), "closing_value": m(cv)})
    rows.sort(key=lambda r: (r["store"].casefold(), r["item"].casefold()))
    total = {k: m(sum((Decimal(r[k]) for r in rows), ZERO)) for k in ("opening_value", "in_value", "out_cost", "closing_value")}
    return {"from": str(from_date), "to": str(to_date), "rows": rows, "total": total,
            "stores": len({r["store"] for r in rows}), "items": len({r["item_id"] for r in rows})}


def _main_groups_name(main_groups: dict, group_id) -> str | None:
    return main_groups.get(group_id) if group_id else None


# ------------------------------------------------------------------ stock journal register (in / out)

JOURNAL_BASES = ("Stock Journal", "Manufacturing Journal")
DEFAULT_JOURNAL = "Material Transfer to WIP"
WO_MANUFACTURING = ("WO Manufacturing Vouchers", "Manufacturing Journal")
WO_DIVISIONS = ("hydraulic", "coc", "cooler")   # columns Hydraulic, COC, OC; anything else is "other"


def stock_journal_register(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, *,
                           voucher_type: str | tuple | None = None, item: uuid.UUID | None = None,
                           stock_group: uuid.UUID | None = None) -> dict:
    """Stock journals (material transfer to WIP, manufacturing ...) with what came in and what went out of
    each, as two lists side by side. voucher_type "all" takes every stock journal type; none takes
    Material Transfer to WIP when the company has it; a tuple takes those types."""
    types = list(db.scalars(select(VoucherType).where(VoucherType.company_id == company_id,
                                                      VoucherType.base_type.in_(JOURNAL_BASES)).order_by(VoucherType.name)))
    names = {t.name for t in types}
    if isinstance(voucher_type, tuple):
        ids = [t.id for t in types if t.name in voucher_type]
        chosen = ", ".join(voucher_type)
    else:
        chosen = voucher_type if voucher_type and voucher_type != "all" else (
            DEFAULT_JOURNAL if voucher_type is None and DEFAULT_JOURNAL in names else None)
        ids = [t.id for t in types if chosen is None or t.name == chosen]
    groups = _group_names(db, company_id)
    mains = _main_groups(db, company_id)
    rows = db.execute(
        select(VoucherInventoryLine, Voucher, VoucherType.name, StockItem)
        .join(Voucher, Voucher.id == VoucherInventoryLine.voucher_id)
        .join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
        .join(StockItem, StockItem.id == VoucherInventoryLine.stock_item_id)
        .where(Voucher.company_id == company_id, Voucher.status == POSTED, Voucher.voucher_type_id.in_(ids or [None]),
               Voucher.voucher_date.between(from_date, to_date))
        .order_by(Voucher.voucher_date, Voucher.voucher_number, VoucherInventoryLine.line_no)).all()
    keep = None
    if item or stock_group:   # the vouchers that move the item / an item of the group, shown whole
        keep = {v.id for line, v, _t, it in rows if (item and it.id == item) or (stock_group and it.group_id == stock_group)}
    vouchers: dict = {}
    for line, v, vtype, it in rows:
        if keep is not None and v.id not in keep:
            continue
        rec = vouchers.setdefault(v.id, {"id": str(v.id), "date": str(v.voucher_date), "voucher_type": vtype,
                                         "voucher_number": v.voucher_number, "narration": v.narration, "in": [], "out": []})
        inward = line.quantity > 0 or (line.quantity == 0 and line.inward)
        rec["in" if inward else "out"].append({
            "item_id": str(it.id), "item": it.name, "item_group": groups.get(it.group_id),
            "main_group": mains.get(it.group_id), "group_id": str(it.group_id) if it.group_id else None, "quantity": _qty(abs(line.quantity)),
            "unit": line.unit, "rate": m(line.rate), "amount": m(abs(line.amount)), "godown": line.godown, "batch": line.batch})

    def total(lines):
        units = {x["unit"] for x in lines}
        return {"quantity": _qty(sum((Decimal(x["quantity"]) for x in lines), ZERO)) if len(units) == 1 else None,
                "unit": next(iter(units)) if len(units) == 1 else None,
                "amount": m(sum((Decimal(x["amount"]) for x in lines), ZERO)), "items": len(lines)}
    out = []
    for rec in vouchers.values():
        rec["in_total"], rec["out_total"] = total(rec["in"]), total(rec["out"])
        out.append(rec)
    return {"from": str(from_date), "to": str(to_date), "voucher_type": chosen or "all",
            "voucher_types": [t.name for t in types], "vouchers": out,
            "total": {"vouchers": len(out),
                      "in_amount": m(sum((Decimal(r["in_total"]["amount"]) for r in out), ZERO)),
                      "out_amount": m(sum((Decimal(r["out_total"]["amount"]) for r in out), ZERO))}}


def wo_manufacturing(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, *,
                     item: uuid.UUID | None = None, stock_group: uuid.UUID | None = None) -> dict:
    """WO Manufacturing vouchers: the item produced (quantity, item group) and what was consumed for it,
    item-wise under each consumed item group with group totals. The consumption is also split by division
    (Hydraulic, COC, OC) from the stock-group divisions in the MIS settings."""
    from app.reports import mis   # mis builds on this module
    sg_div = {str(k): d for k, (d, _kind) in mis.stock_group_map(mis.load_settings(db, company_id)).items()}
    data = stock_journal_register(db, company_id, from_date, to_date, voucher_type=WO_MANUFACTURING, item=item,
                                  stock_group=stock_group)

    def split(lines):
        out = {k: ZERO for k in (*WO_DIVISIONS, "other")}
        for x in lines:
            out[x["division"]] += Decimal(x["amount"])
        return {k: m(v) for k, v in out.items()}
    period = []
    for v in data["vouchers"]:
        for x in v["out"]:
            d = sg_div.get(x["group_id"])
            x["division"] = d if d in WO_DIVISIONS else "other"
        v["divisions"] = split(v["out"])
        period += v["out"]
        by_group: dict = {}
        for x in v["out"]:
            g = by_group.setdefault(x["item_group"] or "(no group)", {"group": x["item_group"] or "(no group)", "items": []})
            g["items"].append(x)
        groups = []
        for g in sorted(by_group.values(), key=lambda g: g["group"]):
            units = {x["unit"] for x in g["items"]}
            g["quantity"] = _qty(sum((Decimal(x["quantity"]) for x in g["items"]), ZERO)) if len(units) == 1 else None
            g["unit"] = next(iter(units)) if len(units) == 1 else None
            g["amount"] = m(sum((Decimal(x["amount"]) for x in g["items"]), ZERO))
            g["divisions"] = split(g["items"])
            groups.append(g)
        v["consumed_groups"] = groups
    data["total"]["divisions"] = split(period)
    return data
