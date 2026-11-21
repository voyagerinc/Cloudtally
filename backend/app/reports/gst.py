"""GST reports from the books: GSTR-1 (outward), GSTR-2 (inward), HSN summary, e-invoice and e-way bill status.

Each sales / purchase / credit / debit note voucher is read once: its party (GSTIN, registration type,
state), its GST ledgers by duty head (IGST, CGST, SGST/UTGST, Cess), its taxable value (every other ledger
except round-off and TDS/TCS) and, from its item lines, the split by GST rate. Rate-wise tax is the
voucher's tax shared over the rates in proportion to taxable value x rate, as the GST portal expects one
line per rate. Credit notes in GSTR-1 (and debit notes in GSTR-2) are reported in their own sections and
taken off the totals."""
import re
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Ledger, StockItem, Voucher, VoucherEntry, VoucherInventoryLine, VoucherType

ZERO = Decimal(0)
POSTED = "posted"
HEADS = ("igst", "cgst", "sgst", "cess")
SLABS = [Decimal(x) for x in ("0", "0.1", "0.25", "1", "1.5", "3", "5", "6", "7.5", "12", "18", "28")]
OUTWARD = ("Sales", "Credit Note")
INWARD = ("Purchase", "Debit Note")
# E-invoice and e-way bill: the documents a company issues for them, whatever their amount.
DOCUMENT_BASES = ("Sales", "Credit Note", "Debit Note")
ROUND_OFF = re.compile(r"round", re.I)
# Proforma invoices, quotations, estimates and memos are not supplies: never in the GST reports, even when
# their voucher type is a kind of Sales in Tally.
NOT_SUPPLY = re.compile(r"pro[\s-]?forma|quotation|estimate|memo", re.I)
# Common Tally units -> GST unit quantity codes (UQC); anything else is shown as it is.
UQC = {"nos": "NOS", "no": "NOS", "no.": "NOS", "pcs": "PCS", "pc": "PCS", "kg": "KGS", "kgs": "KGS", "gm": "GMS",
       "gms": "GMS", "g": "GMS", "ltr": "LTR", "ltrs": "LTR", "l": "LTR", "mtr": "MTR", "mtrs": "MTR", "m": "MTR",
       "set": "SET", "sets": "SET", "box": "BOX", "pair": "PRS", "prs": "PRS", "doz": "DOZ", "bag": "BAG",
       "bags": "BAG", "roll": "ROL", "rolls": "ROL", "sqm": "SQM", "sqft": "SQF", "ton": "TON", "mt": "MTS",
       "unit": "UNT", "units": "UNT", "bottle": "BTL", "btl": "BTL", "can": "CAN", "pkt": "PAC", "packet": "PAC"}


def m(v) -> str:
    return f"{Decimal(v).quantize(Decimal('0.01')):f}"


def _head(led: Ledger, under_duties: bool = False) -> str | None:
    """IGST / CGST / SGST / CESS for a GST ledger, else None. Without a duty head set in Tally, the name of a
    GST ledger (or of any ledger under Duties & Taxes) tells it."""
    h = (led.gst_duty_head or "").upper()
    if not h and ((led.tax_type or "").upper() == "GST" or under_duties):
        h = led.name.upper()
    for key, name in (("IGST", "igst"), ("CGST", "cgst"), ("SGST", "sgst"), ("UTGST", "sgst"), ("CESS", "cess")):
        if key in h:
            return name
    return None


def _snap(rate: Decimal) -> Decimal:
    best = min(SLABS, key=lambda s: abs(s - rate))
    return best if abs(best - rate) <= Decimal("0.6") else rate.quantize(Decimal("0.01"))


def _uqc(unit: str | None) -> str:
    return UQC.get((unit or "").strip().lower(), (unit or "OTH").strip().upper()[:3] or "OTH")


def _context(db: Session, company_id: uuid.UUID) -> dict:
    from app.reports import _groups
    groups = _groups(db, company_id)
    ledgers = {led.id: led for led in db.scalars(select(Ledger).where(Ledger.company_id == company_id))}
    duties = {gid for gid, g in groups.items() if "Duties & Taxes" in g["path"]}
    heads = {i: h for i, led in ledgers.items() if (h := _head(led, led.group_id in duties))}
    other_tax = {i for i, led in ledgers.items() if i not in heads and (
        (led.tax_type and led.tax_type not in ("Others", "GST")) or re.search(r"\bTDS\b|\bTCS\b", led.name, re.I))}
    party_groups = {gid for gid, g in groups.items() if {"Sundry Debtors", "Sundry Creditors"} & set(g["path"])}
    return {"ledgers": ledgers, "heads": heads, "other_tax": other_tax,
            "round": {i for i, led in ledgers.items() if ROUND_OFF.search(led.name)},
            "party_ids": {i for i, led in ledgers.items() if led.group_id in party_groups}}


def invoices(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, side: str = "outward",
             bases: tuple | None = None) -> list[dict]:
    """Every GST document of the period with its party, values, taxes and rate-wise split."""
    bases = bases or (OUTWARD if side == "outward" else INWARD)
    ctx = _context(db, company_id)
    led = ctx["ledgers"]
    rows = db.execute(select(Voucher, VoucherType.base_type, VoucherType.name)
                      .join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
                      .where(Voucher.company_id == company_id, Voucher.status == POSTED,
                             VoucherType.affects_accounts.is_(True), VoucherType.base_type.in_(bases),
                             Voucher.voucher_date.between(from_date, to_date))
                      .order_by(Voucher.voucher_date, Voucher.voucher_number)).all()
    rows = [r for r in rows if not NOT_SUPPLY.search(r[2])]
    ids = [v.id for v, _, _ in rows]
    entries, lines = defaultdict(list), defaultdict(list)
    for n in range(0, len(ids), 5000):
        part = ids[n:n + 5000]
        for e in db.scalars(select(VoucherEntry).where(VoucherEntry.voucher_id.in_(part))):
            entries[e.voucher_id].append(e)
        for line, it in db.execute(select(VoucherInventoryLine, StockItem)
                                   .join(StockItem, StockItem.id == VoucherInventoryLine.stock_item_id)
                                   .where(VoucherInventoryLine.voucher_id.in_(part))):
            lines[line.voucher_id].append((line, it))
    out = []
    for v, base, vtype in rows:
        es = entries[v.id]
        party_id = v.party_ledger_id
        if party_id is None:   # the debtor / creditor line, else the line with a GSTIN, else the biggest
            cands = [e for e in es if e.ledger_id in ctx["party_ids"]] or [e for e in es if led[e.ledger_id].gstin]
            party_id = max(cands or es, key=lambda e: abs(e.amount)).ledger_id if es else None
        party = led.get(party_id)
        # The invoice is what stands against the party: for a sale its credit side (sales, charges, GST,
        # round-off). Lines on the party's own side (an advance adjusted, TDS) settle it and are not supplies.
        party_net = sum((e.amount for e in es if e.ledger_id == party_id), ZERO)
        party_dr = party_net > 0 if party_net else base in ("Sales", "Debit Note")
        tax = dict.fromkeys(HEADS, ZERO)
        taxable = rounding = ZERO
        for e in es:
            if e.ledger_id == party_id or not e.amount:
                continue
            against = (e.amount < 0) == party_dr          # on the other side from the party
            if e.ledger_id in ctx["round"]:
                rounding += abs(e.amount) if against else -abs(e.amount)
            elif not against or e.ledger_id in ctx["other_tax"]:
                continue
            elif e.ledger_id in ctx["heads"]:
                tax[ctx["heads"][e.ledger_id]] += abs(e.amount)
            else:
                taxable += abs(e.amount)
        total_tax = sum(tax.values(), ZERO)
        value = taxable + total_tax + rounding
        # Rate-wise taxable value: item lines by their GST rate, charges at the highest rate of the voucher.
        by_rate: dict[Decimal, Decimal] = defaultdict(lambda: ZERO)
        its = lines[v.id]
        for line, it in its:
            if it.gst_rate is not None:
                by_rate[Decimal(str(it.gst_rate))] += abs(line.amount)
        inferred = _snap(total_tax / taxable * 100) if taxable else ZERO
        if not by_rate:
            by_rate[inferred] = taxable
        else:
            # charges (freight, packing) and items without a rate: at the voucher's highest rate
            by_rate[max(by_rate)] += max(taxable - sum(by_rate.values(), ZERO), ZERO)
        weights = {r: b * r for r, b in by_rate.items()}
        total_w = sum(weights.values(), ZERO)
        rates = []
        for r, b in sorted(by_rate.items()):
            share = (weights[r] / total_w) if total_w else (Decimal(1) if len(by_rate) == 1 else ZERO)
            rates.append({"rate": m(r), "taxable": m(b), **{h: m(tax[h] * share) for h in HEADS}})
        gstin = (party.gstin or "").strip().upper() if party else ""
        reg = (party.gst_registration_type or "") if party else ""
        country = (party.country or "") if party else ""
        export = (country and country.strip().lower() not in ("india", "in")) or reg.lower() == "overseas" or \
            (v.place_of_supply or "").strip().lower() in ("other country", "other territory")
        note = base in ("Credit Note", "Debit Note")
        inter = tax["igst"] > 0
        registered = bool(gstin) and reg.lower() not in ("unregistered", "consumer", "regular-unregistered")
        if side == "outward":
            if note:
                section = "CDNR" if registered else "CDNUR"
            elif export:
                section = "EXP"
            elif registered:
                section = "B2B"
            elif inter and abs(value) > (Decimal(100000) if v.voucher_date >= date(2024, 8, 1) else Decimal(250000)):
                section = "B2CL"
            else:
                section = "B2CS"
        else:
            section = ("CDN" if registered else "CDNUR") if note else "IMP" if export else "B2B" if registered else "B2BUR"
        st = v.statutory or {}
        out.append({
            "id": str(v.id), "date": str(v.voucher_date), "number": v.voucher_number, "voucher_type": vtype,
            "doc": "Note" if note else "Invoice", "base_type": base, "section": section,
            "party": party.name if party else "", "gstin": gstin, "registration_type": reg,
            "party_state": (party.state or "") if party else "", "place_of_supply": v.place_of_supply or "",
            "reverse_charge": "Y" if v.is_reverse_charge else "N", "inter_state": inter,
            "value": m(value), "taxable": m(taxable), **{h: m(tax[h]) for h in HEADS},
            "total_tax": m(total_tax), "rates": rates,
            "irn": st.get("irn") or "", "ack_no": st.get("ack_no") or "", "ack_date": st.get("ack_date") or "",
            "eway_no": st.get("eway_bill_no") or "", "eway_date": st.get("eway_bill_date") or "",
            "has_goods": bool(its), "_st": st, "_lines": its, "_tax": tax, "_weights": weights, "_total_w": total_w,
        })
    return out


def _public(inv: dict) -> dict:
    return {k: v for k, v in inv.items() if not k.startswith("_")}


SECTION_LABELS = {
    "B2B": "B2B invoices (registered)", "B2CL": "B2C large (inter-state, unregistered)",
    "B2CS": "B2C small (unregistered)", "EXP": "Exports", "CDNR": "Credit / debit notes (registered)",
    "CDNUR": "Credit / debit notes (unregistered)", "B2BUR": "Purchases from unregistered",
    "CDN": "Debit / credit notes (registered)", "IMP": "Imports",
}


def gstr(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, side: str = "outward") -> dict:
    """GSTR-1 (outward) or GSTR-2 (inward): sections with their documents, and one row per document and rate."""
    docs = invoices(db, company_id, from_date, to_date, side)
    sections: dict = {}
    total = dict.fromkeys(("value", "taxable", *HEADS), ZERO)
    rows = []
    for d in docs:
        s = sections.setdefault(d["section"], {"section": d["section"], "label": SECTION_LABELS.get(d["section"], d["section"]),
                                               "count": 0, **dict.fromkeys(("value", "taxable", *HEADS), ZERO)})
        sign = -1 if d["doc"] == "Note" else 1
        s["count"] += 1
        for k in ("value", "taxable", *HEADS):
            s[k] += Decimal(d[k]) * sign
            total[k] += Decimal(d[k]) * sign
        for r in d["rates"]:
            rows.append({"section": d["section"], "gstin": d["gstin"], "party": d["party"], "voucher_type": d["voucher_type"],
                         "number": d["number"], "date": d["date"], "value": d["value"], "place_of_supply": d["place_of_supply"],
                         "reverse_charge": d["reverse_charge"], "rate": r["rate"], "taxable": r["taxable"],
                         **{h: r[h] for h in HEADS}, "irn": d["irn"]})
    order = ["B2B", "B2CL", "B2CS", "EXP", "CDNR", "CDNUR", "B2BUR", "IMP", "CDN"]
    return {"from": str(from_date), "to": str(to_date), "side": side,
            "sections": [{k: (m(v) if isinstance(v, Decimal) else v) for k, v in sections[s].items()}
                         for s in sorted(sections, key=lambda x: order.index(x) if x in order else 99)],
            "documents": [_public(d) for d in docs], "rows": rows, "total": {k: m(v) for k, v in total.items()}}


def hsn_summary(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, side: str = "outward") -> dict:
    """HSN-wise quantity, value and tax of the item lines (notes taken off), split B2B / B2C as the portal asks."""
    docs = invoices(db, company_id, from_date, to_date, side)
    acc: dict = {}
    no_hsn = set()
    for d in docs:
        sign = -1 if d["doc"] == "Note" else 1
        kind = "B2B" if d["section"] in ("B2B", "CDNR", "CDN") else "B2C"
        for line, it in d["_lines"]:
            rate = Decimal(str(it.gst_rate)) if it.gst_rate is not None else None
            amt = abs(line.amount)
            w = amt * (rate if rate is not None else (max((r for r in d["_weights"]), default=ZERO)))
            share = (w / d["_total_w"]) if d["_total_w"] else ZERO
            hsn = (it.hsn_code or "").strip() or "(no HSN)"
            if hsn == "(no HSN)":
                no_hsn.add(it.name)
            key = (kind, hsn, _uqc(line.unit or it.unit), m(rate) if rate is not None else "")
            row = acc.setdefault(key, {"type": kind, "hsn": hsn, "description": (it.hsn_description or "")[:60],
                                       "uqc": key[2], "rate": key[3], "quantity": ZERO, "taxable": ZERO,
                                       **dict.fromkeys(HEADS, ZERO), "items": set()})
            row["quantity"] += abs(line.quantity) * sign
            row["taxable"] += amt * sign
            for h in HEADS:
                row[h] += d["_tax"][h] * share * sign
            row["items"].add(it.name)
    rows = []
    total = dict.fromkeys(("taxable", "value", *HEADS), ZERO)
    for key in sorted(acc, key=lambda k: (k[0], k[1], k[3])):
        r = acc[key]
        tax = sum((r[h] for h in HEADS), ZERO)
        value = r["taxable"] + tax
        for k, v in (("taxable", r["taxable"]), ("value", value), *((h, r[h]) for h in HEADS)):
            total[k] += v
        rows.append({"type": r["type"], "hsn": r["hsn"], "description": r["description"], "uqc": r["uqc"],
                     "quantity": f"{r['quantity'].normalize():f}", "value": m(value), "rate": r["rate"],
                     "taxable": m(r["taxable"]), **{h: m(r[h]) for h in HEADS}, "items": len(r["items"])})
    return {"from": str(from_date), "to": str(to_date), "side": side, "rows": rows,
            "total": {k: m(v) for k, v in total.items()}, "items_without_hsn": sorted(no_hsn)[:200],
            "items_without_hsn_count": len(no_hsn)}


def einvoice(db: Session, company_id: uuid.UUID, from_date: date, to_date: date) -> dict:
    """E-invoice status of sales invoices, credit notes and debit notes: IRN generated, missing (B2B, exports,
    notes to registered parties) or not needed."""
    docs = invoices(db, company_id, from_date, to_date, "outward", DOCUMENT_BASES)
    rows, counts = [], defaultdict(int)
    for d in docs:
        needed = d["section"] in ("B2B", "EXP", "CDNR")
        status = "Generated" if d["irn"] else ("Missing" if needed else "Not applicable")
        counts[status] += 1
        rows.append({"date": d["date"], "voucher_type": d["voucher_type"], "number": d["number"], "party": d["party"],
                     "gstin": d["gstin"], "section": d["section"], "value": d["value"], "taxable": d["taxable"],
                     "total_tax": d["total_tax"], "status": status, "irn": d["irn"], "ack_no": d["ack_no"],
                     "ack_date": d["ack_date"], "id": d["id"]})
    return {"from": str(from_date), "to": str(to_date), "rows": rows, "counts": dict(counts)}


def eway_bill(db: Session, company_id: uuid.UUID, from_date: date, to_date: date) -> dict:
    """Every sales invoice, credit note and debit note with the e-way bill Tally has for it. No amount limit:
    some companies make an e-way bill for any invoice, even of one rupee."""
    rows, counts = [], defaultdict(int)
    for d in invoices(db, company_id, from_date, to_date, "outward", DOCUMENT_BASES):
        st = d["_st"]
        status = "Cancelled" if st.get("eway_cancelled") else "Generated" if d["eway_no"] else "Not generated"
        counts[status] += 1
        route = " → ".join(x for x in (st.get("eway_from_place") or st.get("eway_from_state"),
                                       st.get("eway_to_place") or st.get("eway_to_state")) if x)
        rows.append({"date": d["date"], "voucher_type": d["voucher_type"], "number": d["number"], "party": d["party"],
                     "gstin": d["gstin"], "place_of_supply": d["place_of_supply"], "value": d["value"],
                     "status": status, "eway_no": d["eway_no"], "eway_date": d["eway_date"],
                     "valid_upto": st.get("eway_valid_upto", ""), "route": route, "to_pincode": st.get("eway_to_pincode", ""),
                     "transporter": st.get("transporter", ""), "transporter_id": st.get("transporter_id", ""),
                     "vehicle_no": st.get("vehicle_no", ""), "transport_mode": st.get("transport_mode", ""),
                     "distance_km": st.get("distance_km", ""), "id": d["id"]})
    return {"from": str(from_date), "to": str(to_date), "rows": rows, "counts": dict(counts)}
