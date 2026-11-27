"""Management (MIS) reports by division: P&L, expenses, expense breakup and deferred expenses.

Every P&L amount is given to a division in this order:
  1. the cost centres it is allocated to in Tally (each cost centre maps to a division, or to "shared");
  2. otherwise the ledger's own division, when one is set for it;
  3. otherwise, for sales and purchases, the stock groups of the items on the voucher;
  4. otherwise it is shared.
Shared amounts are split by the ledger's percentages (equal parts unless set).

Settings (MisSettings.config):
  divisions:      [{"key": "cooler", "name": "Cooler"}, ...]
  cost_centres:   {"OIL COOLER": "cooler", "H.O": "shared", ...}
  stock_groups:   {"<group id>": {"division": "cooler" | None, "kind": "rm" | "wip" | "fg" | None}}   (sub-groups inherit)
  ledgers:        {"<ledger id>": {"class": ..., "head": ..., "division": "<key>" | "shared" | None,
                                   "split": {"cooler": 20, ...}, "deferred": "add" | "replace"}}
  stock_overrides: {"2026-10": {"cooler": {"rm": {"opening": "123.00", "closing": "456.00"}}}}
"""
import re
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CostAllocation, CostCentre, Ledger, StockGroup, StockItem, Voucher, VoucherEntry, VoucherInventoryLine, VoucherType,
)
from app.models.base import ZERO
from app.models.mis import DeferredExpense, MisSettings
from app.models.vouchers import VoucherStatus
from app.services.canonical import m

DEFAULT_DIVISIONS = [{"key": "cooler", "name": "Cooler"}, {"key": "hydraulic", "name": "Hydraulic"},
                     {"key": "coc", "name": "Customised Cooler"}]
SHARED = "shared"
KINDS = ("rm", "wip", "fg")
# What a ledger's amount is in the MIS P&L, from its primary group in Tally.
CLASS_OF_PRIMARY = {"Sales Accounts": "sales", "Purchase Accounts": "purchase", "Direct Incomes": "direct_income",
                    "Indirect Incomes": "other_income", "Direct Expenses": "factory", "Indirect Expenses": "admin"}
CLASSES = {"sales": "Sales", "purchase": "Purchases", "direct_income": "Direct income", "other_income": "Other income",
           "factory": "Factory overheads", "admin": "Admin overheads", "ignore": "Not in the MIS P&L"}
INCOME = {"sales", "direct_income", "other_income"}
MEMO_TYPE = re.compile(r"proforma|quotation|estimate|memo", re.I)


# ------------------------------------------------------------------ settings

def _guess_division(name: str, divisions: list[dict]) -> str | None:
    n = f" {name.upper()} "
    keys = {d["key"] for d in divisions}
    if "hydraulic" in keys and re.search(r"HYD", n):
        return "hydraulic"
    if "coc" in keys and re.search(r"\bCOC\b|CUSTOM", n):
        return "coc"
    if "cooler" in keys and re.search(r"COOLER|\bOC\b", n):
        return "cooler"
    return None


def _guess_kind(name: str) -> str | None:
    n = name.upper()
    if "WIP" in n or "WORK IN PROGRESS" in n:
        return "wip"
    if "RAW MATERIAL" in n or re.search(r"\bRM\b", n):
        return "rm"
    if "FINISH" in n:
        return "fg"
    return None


def _company_groups(db: Session, company_id):
    from app.reports import _groups
    return _groups(db, company_id)


def load_settings(db: Session, company_id: uuid.UUID) -> dict:
    """Saved settings with defaults filled in: guessed from cost-centre, stock-group and ledger names."""
    row = db.get(MisSettings, company_id)
    cfg = dict(row.config) if row else {}
    divisions = cfg.get("divisions") or DEFAULT_DIVISIONS
    centres = {c.name for c in db.scalars(select(CostCentre).where(CostCentre.company_id == company_id))}
    centres |= set(db.scalars(select(CostAllocation.cost_centre).join(VoucherEntry, VoucherEntry.id == CostAllocation.entry_id)
                              .join(Voucher).where(Voucher.company_id == company_id).distinct()))
    saved_cc = cfg.get("cost_centres") or {}
    cost_centres = {c: saved_cc.get(c) or _guess_division(c, divisions) or SHARED for c in sorted(centres)}

    groups = list(db.scalars(select(StockGroup).where(StockGroup.company_id == company_id)))
    saved_sg = cfg.get("stock_groups") or {}
    explicit = {}
    for g in groups:
        s = saved_sg.get(str(g.id))
        if s is not None:
            explicit[g.id] = {"division": s.get("division"), "kind": s.get("kind"), "saved": True}
        else:
            div, kind = _guess_division(g.name, divisions), _guess_kind(g.name)
            if div or kind:
                explicit[g.id] = {"division": div, "kind": kind, "saved": False}
    return {"divisions": divisions, "cost_centres": cost_centres, "stock_groups_explicit": explicit,
            "ledgers": cfg.get("ledgers") or {}, "stock_overrides": cfg.get("stock_overrides") or {},
            "_groups": {g.id: g for g in groups}}


def item_map(db: Session, company_id, settings: dict) -> dict:
    """{item id: (division, kind)}: the sheet's own map when it has one (the category-wise sheet), else from
    the stock groups' settings."""
    if settings.get("_item_dk") is not None:
        return settings["_item_dk"]
    sg = stock_group_map(settings)
    return {iid: sg.get(gid, (None, None)) for iid, gid in db.execute(
        select(StockItem.id, StockItem.group_id).where(StockItem.company_id == company_id))}


def stock_group_map(settings: dict) -> dict:
    """Effective (division, kind) of every stock group: its own setting, else its nearest parent's, per field."""
    groups, explicit = settings["_groups"], settings["stock_groups_explicit"]
    out = {}

    def resolve(gid, seen=()):
        if gid in out:
            return out[gid]
        g = groups.get(gid)
        own = explicit.get(gid, {})
        parent = resolve(g.parent_id, seen + (gid,)) if g and g.parent_id in groups and g.parent_id not in seen else (None, None)
        res = (own.get("division") or parent[0], own.get("kind") or parent[1])
        out[gid] = res
        return res
    for gid in groups:
        resolve(gid)
    return out


def save_settings(db: Session, company_id: uuid.UUID, changes: dict, actor: str) -> dict:
    row = db.get(MisSettings, company_id)
    if row is None:
        row = MisSettings(company_id=company_id, config={})
        db.add(row)
    cfg = dict(row.config or {})
    for key in ("divisions", "cost_centres", "stock_groups", "ledgers", "stock_overrides"):
        if key in changes and changes[key] is not None:
            if key in ("divisions",):
                cfg[key] = changes[key]
            else:
                merged = dict(cfg.get(key) or {})
                for k, v in changes[key].items():
                    if v is None:
                        merged.pop(k, None)
                    else:
                        merged[k] = v
                cfg[key] = merged
    row.config = cfg
    row.updated_by = actor
    return cfg


def ledger_setting(settings: dict, ledger_id) -> dict:
    return settings["ledgers"].get(str(ledger_id)) or {}


def ledger_class(ledger, groups: dict, settings: dict) -> str | None:
    s = ledger_setting(settings, ledger.id)
    if s.get("class"):
        return s["class"]
    g = groups.get(ledger.group_id)
    return CLASS_OF_PRIMARY.get(g["primary"].name) if g else None


def ledger_head(ledger, groups: dict, settings: dict) -> str:
    s = ledger_setting(settings, ledger.id)
    if s.get("head"):
        return s["head"]
    g = groups.get(ledger.group_id)
    return g["group"].name if g else ledger.name


def split_of(settings: dict, ledger_id) -> dict:
    """Shares of a shared amount per division (fractions adding up to 1)."""
    keys = [d["key"] for d in settings["divisions"]]
    raw = ledger_setting(settings, ledger_id).get("split") or {}
    pct = {k: Decimal(str(raw.get(k) or 0)) for k in keys}
    total = sum(pct.values(), ZERO)
    if total <= 0:
        if settings.get("_split"):         # the category-wise sheet: by each category's share of sales
            return settings["_split"]
        return {k: Decimal(1) / len(keys) for k in keys}
    return {k: pct[k] / total for k in keys}


# ------------------------------------------------------------------ amounts by ledger and division

def months_in(from_date: date, to_date: date) -> list[date]:
    out, d = [], from_date.replace(day=1)
    while d <= to_date:
        out.append(d)
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def ledger_amounts(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, settings: dict) -> dict:
    """{ledger_id: {"class", "head", "by": {division: amount}, "return": {division: amount}}} for P&L ledgers.
    Amounts are positive for income (sales, incomes) and for expenses; "return" holds sales returns."""
    groups = _company_groups(db, company_id)
    ledgers = {l.id: l for l in db.scalars(select(Ledger).where(Ledger.company_id == company_id))}
    classes = {lid: ledger_class(l, groups, settings) for lid, l in ledgers.items()}
    wanted = {lid for lid, c in classes.items() if c and c != "ignore"}
    if not wanted:
        return {}
    keys = [d["key"] for d in settings["divisions"]]
    cc_map = settings["cost_centres"]
    item_div = {iid: dk[0] for iid, dk in item_map(db, company_id, settings).items()}

    rows = db.execute(
        select(VoucherEntry.id, VoucherEntry.voucher_id, VoucherEntry.ledger_id, VoucherEntry.amount,
               VoucherType.base_type, VoucherType.name)
        .join(Voucher, Voucher.id == VoucherEntry.voucher_id)
        .join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
        .where(Voucher.company_id == company_id, Voucher.status == VoucherStatus.POSTED,
               VoucherType.affects_accounts.is_(True),       # not orders, delivery notes ... (as the P&L)
               Voucher.voucher_date.between(from_date, to_date), VoucherEntry.ledger_id.in_(wanted))).all()
    rows = [r for r in rows if not MEMO_TYPE.search(r[5])]
    entry_ids = [r[0] for r in rows]
    allocs = defaultdict(list)
    for n in range(0, len(entry_ids), 5000):
        for eid, centre, amount in db.execute(select(CostAllocation.entry_id, CostAllocation.cost_centre, CostAllocation.amount)
                                              .where(CostAllocation.entry_id.in_(entry_ids[n:n + 5000]))):
            allocs[eid].append((centre, amount))
    # items on sales/purchase vouchers, to place amounts that have no cost centre
    trade_vouchers = list({r[1] for r in rows if classes[r[2]] in ("sales", "purchase")})
    lines = defaultdict(list)
    for n in range(0, len(trade_vouchers), 5000):
        for vid, iid, lid, amount in db.execute(
                select(VoucherInventoryLine.voucher_id, VoucherInventoryLine.stock_item_id, VoucherInventoryLine.ledger_id,
                       VoucherInventoryLine.amount).where(VoucherInventoryLine.voucher_id.in_(trade_vouchers[n:n + 5000]))):
            lines[vid].append((item_div.get(iid), lid, abs(amount)))

    out = {}
    for eid, vid, lid, amount, base, _vtype in rows:
        cls = classes[lid]
        sign = Decimal(-1) if cls in INCOME else Decimal(1)
        value = amount * sign
        rec = out.setdefault(lid, {"class": cls, "head": ledger_head(ledgers[lid], groups, settings),
                                   "name": ledgers[lid].name,
                                   "primary": groups[ledgers[lid].group_id]["primary"].name if ledgers[lid].group_id in groups else None,
                                   "by": defaultdict(lambda: ZERO), "return": defaultdict(lambda: ZERO)})
        bucket = rec["return"] if cls == "sales" and base in ("Credit Note",) else rec["by"]
        if bucket is rec["return"]:
            value = -value   # a return reduces sales; shown as a positive return
        placed = defaultdict(lambda: ZERO)
        rest = value
        for centre, a in allocs.get(eid, ()):
            share = abs(a) / abs(amount) * value if amount else ZERO
            placed[cc_map.get(centre) or SHARED] += share
            rest -= share
        if rest:
            fixed = ledger_setting(settings, lid).get("division")
            if fixed:
                placed[fixed] += rest
            elif cls in ("sales", "purchase") and lines.get(vid):
                own = [x for x in lines[vid] if x[1] == lid] or lines[vid]
                total = sum((x[2] for x in own), ZERO)
                if total:
                    for div, _lid, a in own:
                        placed[div or SHARED] += rest * a / total
                else:
                    placed[SHARED] += rest
            else:
                placed[SHARED] += rest
        shares = split_of(settings, lid)
        for div, v in placed.items():
            if div == SHARED or div not in keys:
                for k in keys:
                    bucket[k] += v * shares[k]
            else:
                bucket[div] += v
    return out


# ------------------------------------------------------------------ deferred expenses

def _add_months(d: date, n: int) -> date:
    y, mth = divmod(d.month - 1 + n, 12)
    return date(d.year + y, mth + 1, 1)


def deferred_charge(s: DeferredExpense, month: date) -> Decimal:
    month = month.replace(day=1)
    if s.months <= 0 or month < s.first_month or month >= _add_months(s.first_month, s.months):
        return ZERO
    return Decimal(s.total_amount) / s.months


def deferred_balance(s: DeferredExpense, month: date) -> Decimal:
    """Still to be charged after the given month (the prepaid amount in the balance sheet at its end)."""
    month = month.replace(day=1)
    if month < _add_months(s.first_month, -1):
        return ZERO
    done = sum(1 for k in range(s.months) if _add_months(s.first_month, k) <= month)
    return Decimal(s.total_amount) - Decimal(s.total_amount) / s.months * done if s.months else ZERO


def deferred_by_ledger(db: Session, company_id, from_date: date, to_date: date) -> dict:
    """{ledger_id or head: amount} charged in the period."""
    out = defaultdict(lambda: ZERO)
    months = months_in(from_date, to_date)
    for s in db.scalars(select(DeferredExpense).where(DeferredExpense.company_id == company_id)):
        amount = sum((deferred_charge(s, mo) for mo in months), ZERO)
        if amount:
            out[(s.ledger_id, s.head)] += amount
    return out


def deferred_report(db: Session, company_id: uuid.UUID, from_date: date, to_date: date) -> dict:
    months = months_in(from_date, to_date)
    rows = []
    heads = defaultdict(lambda: {"charge": [ZERO] * len(months), "balance": [ZERO] * len(months)})
    for s in db.scalars(select(DeferredExpense).where(DeferredExpense.company_id == company_id)
                        .order_by(DeferredExpense.head, DeferredExpense.first_month)):
        charge = [deferred_charge(s, mo) for mo in months]
        balance = [deferred_balance(s, mo) for mo in months]
        h = heads[s.head]
        h["charge"] = [a + b for a, b in zip(h["charge"], charge)]
        h["balance"] = [a + b for a, b in zip(h["balance"], balance)]
        last = _add_months(s.first_month, s.months - 1)
        rows.append({"id": str(s.id), "head": s.head, "ledger_id": str(s.ledger_id) if s.ledger_id else None,
                     "description": s.description, "total_amount": m(s.total_amount), "first_month": s.first_month.strftime("%Y-%m"),
                     "last_month": last.strftime("%Y-%m"), "months": s.months, "monthly": m(Decimal(s.total_amount) / s.months),
                     "charge": [m(x) for x in charge], "balance": [m(x) for x in balance],
                     "charged_in_period": m(sum(charge, ZERO))})
    head_rows = [{"head": k, "charge": [m(x) for x in v["charge"]], "balance": [m(x) for x in v["balance"]],
                  "charged_in_period": m(sum(v["charge"], ZERO))} for k, v in sorted(heads.items())]
    total = {"charge": [m(sum((Decimal(h["charge"][i]) for h in head_rows), ZERO)) for i in range(len(months))],
             "balance": [m(sum((Decimal(h["balance"][i]) for h in head_rows), ZERO)) for i in range(len(months))]}
    return {"from": str(from_date), "to": str(to_date), "months": [mo.strftime("%Y-%m") for mo in months],
            "schedules": rows, "heads": head_rows, "total": total}


# ------------------------------------------------------------------ expense breakup and expenses

def expense_breakup(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, settings: dict | None = None) -> dict:
    """Each overhead ledger (factory and admin) with its amount, deferred share and split by division."""
    settings = settings or load_settings(db, company_id)
    keys = [d["key"] for d in settings["divisions"]]
    amounts = ledger_amounts(db, company_id, from_date, to_date, settings)
    deferred = deferred_by_ledger(db, company_id, from_date, to_date)
    groups = _company_groups(db, company_id)
    ledgers = {l.id: l for l in db.scalars(select(Ledger).where(Ledger.company_id == company_id))}
    rows = {}
    for lid, rec in amounts.items():
        if rec["class"] not in ("factory", "admin"):
            continue
        tally = sum(rec["by"].values(), ZERO)
        mode = ledger_setting(settings, lid).get("deferred") or "add"
        rows[lid] = {"ledger_id": str(lid), "ledger": ledgers[lid].name, "class": rec["class"], "head": rec["head"],
                     "tally": tally, "by": dict(rec["by"]), "deferred": ZERO, "mode": mode}
    for (lid, head), amount in deferred.items():
        led = ledgers.get(lid) if lid else None
        if led is None:   # a schedule without a ledger: its own admin line
            key = f"head:{head}"
            r = rows.setdefault(key, {"ledger_id": None, "ledger": head, "class": "admin", "head": head, "tally": ZERO,
                                      "by": {k: ZERO for k in keys}, "deferred": ZERO, "mode": "add"})
            shares = {k: Decimal(1) / len(keys) for k in keys}
        else:
            cls = ledger_class(led, groups, settings)
            r = rows.setdefault(lid, {"ledger_id": str(lid), "ledger": led.name, "class": cls if cls in ("factory", "admin") else "admin",
                                      "head": ledger_head(led, groups, settings), "tally": ZERO, "by": {k: ZERO for k in keys},
                                      "deferred": ZERO, "mode": ledger_setting(settings, lid).get("deferred") or "add"})
            shares = split_of(settings, lid)
        r["deferred"] += amount
        r.setdefault("deferred_by", defaultdict(lambda: ZERO))
        for k in keys:
            r["deferred_by"][k] += amount * shares[k]
    out = []
    for r in rows.values():
        dby = r.get("deferred_by") or {k: ZERO for k in keys}
        base = {k: ZERO for k in keys} if r["mode"] == "replace" and r["deferred"] else {k: r["by"].get(k, ZERO) for k in keys}
        by = {k: base[k] + dby.get(k, ZERO) for k in keys}
        total = sum(by.values(), ZERO)
        if not total and not r["tally"] and not r["deferred"]:
            continue
        s = ledger_setting(settings, r["ledger_id"]) if r["ledger_id"] else {}
        out.append({"ledger_id": r["ledger_id"], "ledger": r["ledger"], "class": r["class"], "head": r["head"],
                    "tally": m(r["tally"]), "deferred": m(r["deferred"]), "deferred_mode": r["mode"],
                    # the amount is the sum of its rounded division figures, so every total adds up to the paisa
                    "amount": m(sum((Decimal(m(v)) for v in by.values()), ZERO)), "by": {k: m(v) for k, v in by.items()},
                    "split": s.get("split"), "division": s.get("division")})
    sections = []
    for cls in ("factory", "admin"):
        items = sorted((x for x in out if x["class"] == cls), key=lambda x: -Decimal(x["amount"]))
        total = sum((Decimal(x["amount"]) for x in items), ZERO)
        for x in items:
            x["pct"] = m(Decimal(x["amount"]) / total * 100) if total else None
        sections.append({"class": cls, "name": CLASSES[cls], "rows": items, "total": m(total),
                         "by": {k: m(sum((Decimal(x["by"][k]) for x in items), ZERO)) for k in keys}})
    return {"from": str(from_date), "to": str(to_date), "divisions": settings["divisions"], "sections": sections}


def expenses(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, settings: dict | None = None,
             breakup: dict | None = None) -> dict:
    """Factory and admin overheads by expense head and division (the breakup summed by head)."""
    settings = settings or load_settings(db, company_id)
    keys = [d["key"] for d in settings["divisions"]]
    breakup = breakup or expense_breakup(db, company_id, from_date, to_date, settings)
    sections = []
    for sec in breakup["sections"]:
        heads = {}
        for r in sec["rows"]:
            h = heads.setdefault(r["head"], {"head": r["head"], "ledgers": 0, "by": {k: ZERO for k in keys}})
            h["ledgers"] += 1
            for k in keys:
                h["by"][k] += Decimal(r["by"][k])
        rows = [{"head": h["head"], "ledgers": h["ledgers"], "by": {k: m(v) for k, v in h["by"].items()},
                 "total": m(sum(h["by"].values(), ZERO))} for h in heads.values()]
        rows.sort(key=lambda x: -Decimal(x["total"]))
        sections.append({"class": sec["class"], "name": sec["name"], "rows": rows, "by": sec["by"], "total": sec["total"]})
    return {"from": breakup["from"], "to": breakup["to"], "divisions": settings["divisions"], "sections": sections}


# ------------------------------------------------------------------ stock values and the P&L

def stock_values(db: Session, company_id: uuid.UUID, on: date, settings: dict) -> dict:
    """{division: {kind: value}} of closing stock on a date, from the stock groups' settings. An item below zero
    (sold or issued before its receipt was entered) counts as nil stock, not as a negative value."""
    from app.reports import stock_summary
    dk = item_map(db, company_id, settings)
    out = defaultdict(lambda: defaultdict(lambda: ZERO))
    for it in stock_summary(db, company_id, on)["items"]:
        div, kind = dk.get(uuid.UUID(it["item_id"]), (None, None))
        if div and kind:
            out[div][kind] += max(Decimal(it["closing_value"]), ZERO)
    return out


# ------------------------------------------------------------------ material used by another division

TRANSFER_TYPES = ("Stock Journal", "Manufacturing Journal")    # base types: production, material transfer


def inter_division(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, settings: dict,
                   detail: bool = False) -> dict:
    """Material one division's production takes from another division's stock (a customised cooler built
    from a cooler and hydraulic parts, say), from the stock journals of the period.

    The value consumed belongs to the division of the item produced: it is added to that division's RM
    consumption (From <division>) and taken off the RM consumption of the division whose stock it left
    (To other divisions), whatever the stock: raw material, WIP or finished goods. So what the divisions
    received adds up to what they gave, and the company's totals do not change.
    Returns {"in": {div: value}, "from": {source div: {div: value}}, "out": {kind: {div: value}},
    "rows": [{from, to, kind, value, vouchers}]}, and with detail=True "lines": each item consumed."""
    keys = [d["key"] for d in settings["divisions"]]
    item_dk = item_map(db, company_id, settings)
    q = (select(Voucher.id, VoucherType.name, VoucherInventoryLine.stock_item_id, VoucherInventoryLine.quantity,
                VoucherInventoryLine.amount, VoucherInventoryLine.inward, Voucher.voucher_date, Voucher.voucher_number,
                VoucherInventoryLine.unit)
         .join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
         .join(VoucherInventoryLine, VoucherInventoryLine.voucher_id == Voucher.id)
         .where(Voucher.company_id == company_id, Voucher.status == VoucherStatus.POSTED,
                Voucher.voucher_date.between(from_date, to_date), VoucherType.base_type.in_(TRANSFER_TYPES)))
    by_voucher = defaultdict(list)
    head = {}
    for vid, vtype, iid, qty, amount, inward, vdate, vno, unit in db.execute(q):
        if not MEMO_TYPE.search(vtype):
            by_voucher[vid].append((iid, qty, amount, inward, unit))
            head[vid] = (vdate, vno, vtype)
    item_names = dict(db.execute(select(StockItem.id, StockItem.name).where(StockItem.company_id == company_id)).all()) \
        if detail else {}
    detail_lines = []
    t_in = {k: ZERO for k in keys}
    t_from = {s: {k: ZERO for k in keys} for s in keys}
    t_out = {kind: {k: ZERO for k in keys} for kind in KINDS}
    flows = defaultdict(lambda: [ZERO, set()])
    for vid, lines in by_voucher.items():
        produced = defaultdict(lambda: ZERO)        # division of what was produced -> its value
        made = defaultdict(list)                   # division -> the items it produced (for the detail)
        consumed = []
        for iid, qty, amount, inward, unit in lines:
            is_in = qty > 0 if qty else bool(inward)
            div, kind = item_dk.get(iid, (None, None))
            if is_in:
                if div in keys:
                    produced[div] += abs(amount) or Decimal(1)
                    made[div].append(iid)
            elif div in keys and amount:
                consumed.append((div, kind if kind in KINDS else "rm", abs(amount), iid, qty, unit))
        total = sum(produced.values(), ZERO)
        if not total or not consumed:
            continue
        for src, kind, value, iid, qty, unit in consumed:
            for dst, weight in produced.items():
                if dst == src:
                    continue
                v = (value * weight / total).quantize(Decimal("0.01"))
                t_in[dst] += v
                t_from[src][dst] += v
                t_out[kind][src] += v
                if detail:
                    vdate, vno, vtype = head[vid]
                    detail_lines.append({
                        "voucher_id": str(vid), "date": str(vdate), "voucher_number": vno, "voucher_type": vtype,
                        "from_key": src, "to_key": dst, "kind": kind, "item": item_names.get(iid, ""),
                        "quantity": format(abs(qty * weight / total).quantize(Decimal("0.0001")).normalize(), "f") if qty else "", "unit": unit or "",
                        "value": m(v), "produced": sorted({item_names.get(i, "") for i in made[dst]})})
                f = flows[(src, dst, kind)]
                f[0] += v
                f[1].add(vid)
    names = {d["key"]: d["name"] for d in settings["divisions"]}
    rows = [{"from": names[s], "to": names[d], "from_key": s, "to_key": d, "kind": kind, "value": m(v[0]),
             "vouchers": len(v[1])} for (s, d, kind), v in sorted(flows.items(), key=lambda x: -x[1][0]) if v[0]]
    out = {"in": t_in, "from": t_from, "out": t_out, "rows": rows}
    if detail:
        for ln in detail_lines:
            ln["from"], ln["to"] = names[ln["from_key"]], names[ln["to_key"]]
        detail_lines.sort(key=lambda x: (x["date"], x["voucher_number"] or "", -Decimal(x["value"])))
        out["lines"] = detail_lines
    return out


# Blocks of the monthly finance sheet, as laid out in the finance team's workbook: (title, [(column, line key)],
# line key whose % of net revenue is shown)
SHEET_BLOCKS = [
    ("RM Store", [("Opening Stock Value", "rm_open"), ("Purchase Value", "purchases"), ("Closing Stock Value", "rm_close"),
                  ("RM Consumption", "rm_consumption")], "rm_consumption"),
    ("Production Cost (WIP)", [("WIP Opening", "wip_open"), ("RM Consumption", "rm_consumption"),
                               ("Factory Overheads", "factory"), ("Closing WIP Value", "wip_close"), ("COGM", "cogm")], "cogm"),
    ("Finished Goods Cost", [("Opening FG Value", "fg_open"), ("COGM", "cogm"), ("Closing FG Value", "fg_close"),
                             ("COGS", "cogs")], "cogs"),
    ("Sales", [("Sale Value", "sales"), ("Sales Return", "returns"), ("Direct Income", "direct_income"),
               ("Net Revenue", "net_revenue")], None),
    ("P&L", [("Sales Revenue", "net_revenue"), ("COGS", "cogs"), ("Gross Profit", "gross")], "gross"),
    ("Net Profit", [("Gross Profit", "gross"), ("Admin Exp", "admin"), ("Other Income", "other_income"),
                    ("Net Profit", "net")], "net"),
    ("GP P&L", [("Sales Revenue", "net_revenue"), ("RM Cost", "rm_consumption"), ("Gross Profit", "rm_gp")], "rm_gp"),
]


# The same sheet with material used by another division moved to it (see inter_division): a column for
# what came from each division, and one for what went to the others.
def _transfer_blocks(divisions: list[dict]) -> list:
    return [("RM Store", [("Opening Stock Value", "rm_open"), ("Purchase Value", "purchases")]
             + [(f"From {d['name']}", f"rm_in_{d['key']}") for d in divisions]
             + [("To Other Divisions", "rm_out"), ("Closing Stock Value", "rm_close"),
                ("RM Consumption", "rm_consumption")], "rm_consumption")] + SHEET_BLOCKS[1:]


# what a click on a transfer figure shows: the items that went to other divisions (row: the row's division)
DRILL = {"rm_out": {"src": "row"}}


def finance_sheet(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, transfers: bool = False,
                  settings: dict | None = None) -> dict:
    """The P&L in the workbook's own layout: one block per stage, a row per division and a total row, each
    with its % of net revenue; plus the purchase block (purchase, carriage inward, P&F ...).
    transfers=True: material one division's production took from another division is charged to it."""
    pl = profit_and_loss(db, company_id, from_date, to_date, transfers=transfers, settings=settings)
    lines = {ln["key"]: ln for sec in pl["sections"] for ln in sec["lines"]}
    divs = pl["divisions"]
    revenue = lines["net_revenue"]
    period = (from_date.strftime("%b-%y") if pl["month"] else f"{from_date.strftime('%d-%b-%y')} to {to_date.strftime('%d-%b-%y')}")

    def pct(value, base):
        return m(Decimal(value) / Decimal(base) * 100) if Decimal(base) else None
    blocks = []
    for title, cols, pct_key in (_transfer_blocks(divs) if transfers else SHEET_BLOCKS):
        rows = []
        for d in divs:
            row = {"month": period, "category": d["name"], "division": d["key"],
                   "values": [lines[key]["by"][d["key"]] for _, key in cols]}
            row["pct"] = pct(lines[pct_key]["by"][d["key"]], revenue["by"][d["key"]]) if pct_key else None
            rows.append(row)
        total = {"month": "", "category": "Total", "total": True, "values": [lines[key]["total"] for _, key in cols],
                 "pct": pct(lines[pct_key]["total"], revenue["total"]) if pct_key else None}
        editable = {i: lines[key]["editable"] for i, (_, key) in enumerate(cols)
                    if lines[key].get("editable") and not (settings or {}).get("_item_dk")}
        overridden = {i: lines[key].get("overridden", []) for i in editable for key in [cols[i][1]]}
        drill = {}
        if transfers:
            for i, (_, key) in enumerate(cols):
                if key.startswith("rm_in_"):
                    drill[i] = {"src": key[len("rm_in_"):], "dst": "row"}
                elif key in DRILL:
                    drill[i] = DRILL[key]
        blocks.append({"title": title, "columns": [c for c, _ in cols], "pct": pct_key is not None, "rows": rows + [total],
                       "editable": editable, "overridden": overridden, "drill": drill})
    heads = pl["purchase_heads"]
    purchase = {"title": "Purchase", "columns": [h["head"] for h in heads] + ["Total"],
                "rows": [{"category": d["name"], "values": [h["by"][d["key"]] for h in heads] +
                          [m(sum((Decimal(h["by"][d["key"]]) for h in heads), ZERO))]} for d in divs]
                + [{"category": "Total", "total": True,
                    "values": [h["total"] for h in heads] + [m(sum((Decimal(h["total"]) for h in heads), ZERO))]}]}
    return {"from": pl["from"], "to": pl["to"], "period": period, "month": pl["month"], "divisions": divs,
            "basis": (settings or {}).get("basis"),
            "blocks": blocks, "purchase": purchase, "shared_cost_centres": pl["shared_cost_centres"],
            "transfers": pl.get("transfers")}


def profit_and_loss(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, transfers: bool = False,
                    settings: dict | None = None) -> dict:
    """The monthly finance sheet: sales, RM consumption, production cost (WIP), finished goods cost, gross and
    net profit, per division. transfers=True: with material used by another division moved to it."""
    settings = settings or load_settings(db, company_id)
    keys = [d["key"] for d in settings["divisions"]]
    amounts = ledger_amounts(db, company_id, from_date, to_date, settings)
    breakup = expense_breakup(db, company_id, from_date, to_date, settings)
    zero = lambda: {k: ZERO for k in keys}  # noqa: E731

    def total_of(cls, bucket="by"):
        out = zero()
        for rec in amounts.values():
            if rec["class"] == cls:
                for k in keys:
                    out[k] += rec[bucket].get(k, ZERO)
        return out
    sales, returns = total_of("sales"), total_of("sales", "return")
    direct_income, other_income = total_of("direct_income"), total_of("other_income")
    purchases = total_of("purchase")
    factory = {k: Decimal(next(s for s in breakup["sections"] if s["class"] == "factory")["by"][k]) for k in keys}
    admin = {k: Decimal(next(s for s in breakup["sections"] if s["class"] == "admin")["by"][k]) for k in keys}

    opening = stock_values(db, company_id, from_date - timedelta(days=1), settings)
    closing = stock_values(db, company_id, to_date, settings)
    month_key = from_date.strftime("%Y-%m")
    one_month = from_date.day == 1 and months_in(from_date, to_date) == [from_date]
    overrides = settings["stock_overrides"].get(month_key, {}) if one_month else {}
    stock, overridden = {}, []
    for side, values in (("opening", opening), ("closing", closing)):
        for kind in KINDS:
            stock[(side, kind)] = {}
            for k in keys:
                o = (overrides.get(k) or {}).get(kind, {}).get(side)
                if o is not None:
                    stock[(side, kind)][k] = Decimal(str(o))
                    overridden.append(f"{side}:{kind}:{k}")
                else:
                    stock[(side, kind)][k] = values[k][kind]

    def calc(f):
        return {k: f(k) for k in keys}
    moved = inter_division(db, company_id, from_date, to_date, settings) if transfers else None
    # shown as signed amounts, so that each block adds up as it reads (what left a division is negative)
    rm_in = moved["in"] if moved else zero()
    # everything a division gave (raw material, WIP and finished goods) comes off its RM consumption
    rm_out = calc(lambda k: -sum((moved["out"][kind][k] for kind in KINDS), ZERO)) if moved else zero()
    net_revenue = calc(lambda k: sales[k] - returns[k] + direct_income[k])
    rm_consumption = calc(lambda k: stock[("opening", "rm")][k] + purchases[k] + rm_in[k] + rm_out[k]
                          - stock[("closing", "rm")][k])
    cogm = calc(lambda k: stock[("opening", "wip")][k] + rm_consumption[k] + factory[k] - stock[("closing", "wip")][k])
    cogs = calc(lambda k: stock[("opening", "fg")][k] + cogm[k] - stock[("closing", "fg")][k])
    gross = calc(lambda k: net_revenue[k] - cogs[k])
    net = calc(lambda k: gross[k] - admin[k] + other_income[k])
    rm_gp = calc(lambda k: net_revenue[k] - rm_consumption[k])

    def line(key, label, values, *, pct_of=None, strong=False, editable=None, note=None):
        tot = sum(values.values(), ZERO)
        row = {"key": key, "label": label, "by": {k: m(v) for k, v in values.items()}, "total": m(tot), "strong": strong}
        if pct_of is not None:
            base_tot = sum(pct_of.values(), ZERO)
            row["pct"] = {k: (m(values[k] / pct_of[k] * 100) if pct_of[k] else None) for k in keys}
            row["pct_total"] = m(tot / base_tot * 100) if base_tot else None
        if editable:
            row["editable"] = editable
            row["overridden"] = [k for k in keys if f"{editable}:{k}" in overridden]
        if note:
            row["note"] = note
        return row
    sections = [
        {"key": "sales", "name": "Sales", "lines": [
            line("sales", "Sale value", sales), line("returns", "Sales return", returns),
            line("direct_income", "Direct income", direct_income),
            line("net_revenue", "Net revenue", net_revenue, strong=True)]},
        {"key": "rm", "name": "RM store", "lines": [
            line("rm_open", "Opening stock value", stock[("opening", "rm")], editable="opening:rm"),
            line("purchases", "Purchase value", purchases),
            *([line(f"rm_in_{d['key']}", f"From {d['name']}", moved["from"][d["key"]]) for d in settings["divisions"]]
              + [line("rm_in", "From other divisions", rm_in), line("rm_out", "To other divisions", rm_out)]
              if moved else []),
            line("rm_close", "Closing stock value", stock[("closing", "rm")], editable="closing:rm"),
            line("rm_consumption", "RM consumption", rm_consumption, pct_of=net_revenue, strong=True)]},
        {"key": "wip", "name": "Production cost (WIP)", "lines": [
            line("wip_open", "WIP opening", stock[("opening", "wip")], editable="opening:wip"),
            line("rm_consumption2", "RM consumption", rm_consumption),
            line("factory", "Factory overheads", factory),
            line("wip_close", "Closing WIP value", stock[("closing", "wip")], editable="closing:wip"),
            line("cogm", "COGM", cogm, pct_of=net_revenue, strong=True)]},
        {"key": "fg", "name": "Finished goods cost", "lines": [
            line("fg_open", "Opening FG value", stock[("opening", "fg")], editable="opening:fg"),
            line("cogm2", "COGM", cogm),
            line("fg_close", "Closing FG value", stock[("closing", "fg")], editable="closing:fg"),
            line("cogs", "COGS", cogs, pct_of=net_revenue, strong=True)]},
        {"key": "gp", "name": "P&L", "lines": [
            line("revenue", "Sales revenue", net_revenue), line("cogs2", "COGS", cogs),
            line("gross", "Gross profit", gross, pct_of=net_revenue, strong=True)]},
        {"key": "np", "name": "Net profit", "lines": [
            line("gross2", "Gross profit", gross), line("admin", "Admin overheads", admin),
            line("other_income", "Other income", other_income),
            line("net", "Net profit", net, pct_of=net_revenue, strong=True)]},
        {"key": "rmgp", "name": "GP on RM cost", "lines": [
            line("revenue2", "Sales revenue", net_revenue), line("rm_cost", "RM cost", rm_consumption),
            line("rm_gp", "Gross profit", rm_gp, pct_of=net_revenue, strong=True)]},
    ]
    unmapped = sorted({c for c, d in settings["cost_centres"].items() if d == SHARED})
    # what purchases are made of, as in the sheet's purchase block: the purchase accounts, then each other
    # ledger counted as a purchase cost (carriage inward, P&F ...) under its head or name
    heads = {}
    for rec in amounts.values():
        if rec["class"] != "purchase":
            continue
        name = "Purchase" if rec["primary"] == "Purchase Accounts" else (
            rec["head"] if rec["head"] not in CLASS_OF_PRIMARY else rec["name"])
        h = heads.setdefault(name, zero())
        for k in keys:
            h[k] += rec["by"].get(k, ZERO)
    purchase_heads = [{"head": n, "by": {k: m(v) for k, v in h.items()}, "total": m(sum(h.values(), ZERO))}
                      for n, h in sorted(heads.items(), key=lambda x: (x[0] != "Purchase", x[0]))]
    return {"from": str(from_date), "to": str(to_date), "divisions": settings["divisions"], "sections": sections,
            "month": month_key if one_month else None, "purchase_heads": purchase_heads,
            "shared_cost_centres": unmapped, "transfers": moved["rows"] if moved else None}


# ------------------------------------------------------------------ the finance sheet by category, for every company

_FG = re.compile(r"\bFG\b|FINISH", re.I)
_WIP = re.compile(r"\bWIP\b|WORK IN PROGRESS|SEMI", re.I)
_RM = re.compile(r"\bRM\b|RAW MATERIAL", re.I)


def category_settings(db: Session, company_id: uuid.UUID, from_date: date, to_date: date) -> dict:
    """The finance sheet's rows for any company without setting it up: its stock item categories, or, when
    it keeps none, its top stock groups. Raw material / WIP / finished goods: from the stock group settings
    when made, else from the group names (RM, WIP, FG / finished), else an item that is sold is a finished
    good and any other is raw material. Amounts with no category of their own (overheads, a sale without
    items) are shared by each category's share of the period's sales."""
    from app.models import VoucherType as VT
    base = load_settings(db, company_id)
    groups = base["_groups"]
    sg = stock_group_map(base)
    items = db.execute(select(StockItem.id, StockItem.category, StockItem.group_id).where(StockItem.company_id == company_id)).all()
    cats = sorted({(c or "").strip() for _, c, _ in items if (c or "").strip()}, key=str.upper)

    def chain(gid):
        out, seen = [], set()
        while gid in groups and gid not in seen:
            seen.add(gid)
            out.append(groups[gid])
            gid = groups[gid].parent_id
        return out
    basis = "category" if cats else "group"
    # group-wise: the top stock groups, going down a level while one group holds nearly everything (a company
    # whose groups all sit under one group named after it is shown by the groups under that one)
    rp = {iid: [g.name for g in reversed(chain(gid))] for iid, _, gid in items}
    level = {iid: 1 for iid in rp}
    for _ in range(5):
        if basis != "group" or not any(rp.values()):
            break
        counts = defaultdict(int)
        for iid, p in rp.items():
            if p:
                counts[tuple(p[:level[iid]])] += 1
        top, n = max(counts.items(), key=lambda x: x[1])
        deeper = [i for i, p in rp.items() if p and tuple(p[:level[i]]) == top and len(p) > level[i]]
        if n < 0.8 * sum(counts.values()) or not deeper:
            break
        for i in deeper:
            level[i] += 1
    group_name = {iid: (p[level[iid] - 1] if p else "") for iid, p in rp.items()}
    names = cats if cats else sorted({n for n in group_name.values() if n}, key=str.upper)
    key_of = {n: f"d{i}" for i, n in enumerate(names)}
    divisions = [{"key": key_of[n], "name": n} for n in names]
    none_name = "(No category)" if basis == "category" else "(No stock group)"
    sold = set(db.scalars(select(VoucherInventoryLine.stock_item_id).join(Voucher).join(VT, VT.id == Voucher.voucher_type_id)
                          .where(Voucher.company_id == company_id, VT.base_type == "Sales").distinct()))
    item_dk = {}
    for iid, cat, gid in items:
        path = chain(gid)
        name = (cat or "").strip() if basis == "category" else group_name[iid]
        if name:
            div = key_of[name]
        else:
            if not any(d["key"] == "none" for d in divisions):
                divisions.append({"key": "none", "name": none_name})
            div = "none"
        kind = sg.get(gid, (None, None))[1]
        if kind is None:
            text = " ".join(g.name for g in path)
            kind = "wip" if _WIP.search(text) else "fg" if _FG.search(text) else "rm" if _RM.search(text) else None
        item_dk[iid] = (div, kind or ("fg" if iid in sold else "rm"))
    if not divisions:
        divisions = [{"key": "none", "name": none_name}]
    # each category's share of the period's sales, for amounts that have no category of their own
    sales = defaultdict(lambda: ZERO)
    for iid, amount in db.execute(
            select(VoucherInventoryLine.stock_item_id, VoucherInventoryLine.amount).join(Voucher)
            .join(VT, VT.id == Voucher.voucher_type_id)
            .where(Voucher.company_id == company_id, Voucher.status == VoucherStatus.POSTED, VT.base_type == "Sales",
                   Voucher.voucher_date.between(from_date, to_date))):
        sales[item_dk.get(iid, ("none", None))[0]] += abs(amount or ZERO)
    total = sum(sales.values(), ZERO)
    keys = [d["key"] for d in divisions]
    split = {k: (sales[k] / total if total else Decimal(1) / len(keys)) for k in keys}
    return {"divisions": divisions, "cost_centres": {}, "stock_groups_explicit": {}, "ledgers": base["ledgers"],
            "stock_overrides": {}, "_groups": groups, "_item_dk": item_dk, "_split": split, "basis": basis}
