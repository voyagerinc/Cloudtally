"""Canonical voucher/master <-> Tally XML.

Sign convention: canonical amounts are debit-positive; Tally XML amounts are debit-negative
with ISDEEMEDPOSITIVE=Yes for debits. Inventory quantities are inward-positive.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from lxml import etree

from app.connectors.tally import xmlutil as X
from app.services.canonical import m, q

ZERO = Decimal("0")


# ---------------------------------------------------------------- envelopes

def export_collection(collection_type: str, fetch: list[str], company: str | None, *,
                      formula: str | None = None, from_date=None, to_date=None,
                      native_method: list[str] | None = None) -> str:
    name = f"Erp{collection_type.replace(' ', '')}Coll"
    static = "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
    if company:
        static += f"<SVCURRENTCOMPANY>{X.esc(company)}</SVCURRENTCOMPANY>"
    if from_date:
        static += f"<SVFROMDATE>{X.tally_date(from_date)}</SVFROMDATE>"
    if to_date:
        static += f"<SVTODATE>{X.tally_date(to_date)}</SVTODATE>"
    filt = "<FILTER>ErpFilter</FILTER>" if formula else ""
    methods = "".join(f"<NATIVEMETHOD>{X.esc(n)}</NATIVEMETHOD>" for n in native_method or [])
    system = f'<SYSTEM TYPE="Formulae" NAME="ErpFilter">{X.esc(formula)}</SYSTEM>' if formula else ""
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Collection</TYPE><ID>{name}</ID></HEADER><BODY><DESC>"
        f"<STATICVARIABLES>{static}</STATICVARIABLES><TDL><TDLMESSAGE>"
        f'<COLLECTION NAME="{name}" ISMODIFY="No"><TYPE>{X.esc(collection_type)}</TYPE>'
        f"<FETCH>{X.esc(','.join(fetch))}</FETCH>{methods}{filt}</COLLECTION>{system}"
        "</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
    )


def import_envelope(report: str, company: str, body: str) -> str:
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Import</TALLYREQUEST>"
        "<TYPE>Data</TYPE><ID>" + X.esc(report) + "</ID></HEADER><BODY><DESC><STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{X.esc(company)}</SVCURRENTCOMPANY></STATICVARIABLES></DESC>"
        f'<DATA><TALLYMESSAGE xmlns:UDF="TallyUDF">{body}</TALLYMESSAGE></DATA></BODY></ENVELOPE>'
    )


@dataclass
class ImportResult:
    created: int = 0
    altered: int = 0
    deleted: int = 0
    cancelled: int = 0
    ignored: int = 0
    combined: int = 0
    errors: int = 0
    last_vch_id: int | None = None
    last_master_id: int | None = None
    line_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.errors == 0 and not self.line_errors


def parse_import_response(text: str) -> ImportResult:
    root = X.parse(text)

    def num(tag_name: str) -> int:
        el = root.find(f".//{tag_name}")
        try:
            return int((el.text or "0").strip()) if el is not None else 0
        except ValueError:
            return 0

    result = ImportResult(
        created=num("CREATED"), altered=num("ALTERED"), deleted=num("DELETED"), cancelled=num("CANCELLED"),
        ignored=num("IGNORED"), combined=num("COMBINED"), errors=num("ERRORS"),
    )
    result.last_vch_id = num("LASTVCHID") or None
    result.last_master_id = num("LASTMID") or None
    result.line_errors = [e.text.strip() for e in root.iter("LINEERROR") if e.text and e.text.strip()]
    if root.tag == "RESPONSE" and not len(root) and root.text and "unknown request" in root.text.lower():
        result.line_errors.append(root.text.strip())
    return result


# ---------------------------------------------------------------- vouchers

VOUCHER_FETCH = [
    "DATE", "EFFECTIVEDATE", "VOUCHERTYPENAME", "VOUCHERNUMBER", "GUID", "MASTERID", "ALTERID", "REMOTEID",
    "REFERENCE", "REFERENCEDATE", "PARTYLEDGERNAME", "NARRATION", "ISCANCELLED", "ISDELETED", "ISOPTIONAL",
    "ISINVOICE", "PERSISTEDVIEW", "PLACEOFSUPPLY", "ISREVERSECHARGEAPPLICABLE", "IRN", "EWAYBILLNO",
    "ALLLEDGERENTRIES", "LEDGERENTRIES", "ALLINVENTORYENTRIES",
]


@dataclass
class VoucherRef:
    master_id: int
    alter_id: int
    date: object = None


@dataclass
class TallyRef:
    """How to locate an existing Tally object for Alter/Cancel/Delete."""

    master_id: int | None = None
    guid: str | None = None
    remote_id: str | None = None
    alter_by: str = "masterid"   # masterid | guid | remoteid

    def tag_attrs(self) -> str:
        if self.alter_by == "guid" and self.guid:
            return f'TAGNAME="GUID" TAGVALUE={X.attr(self.guid)}'
        if self.alter_by == "remoteid" and self.remote_id:
            return f'REMOTEID={X.attr(self.remote_id)}'
        if self.master_id:
            return f'TAGNAME="MASTERID" TAGVALUE={X.attr(self.master_id)}'
        if self.guid:
            return f'TAGNAME="GUID" TAGVALUE={X.attr(self.guid)}'
        raise ValueError("No Tally identity available for alteration")


def _dr(amount: Decimal) -> str:
    return X.yes_no(amount > 0)


def _ledger_entry_xml(e: dict, list_tag: str, party_ledger: str | None) -> str:
    amt = Decimal(e["amount"])
    parts = [
        X.tag("LEDGERNAME", e["ledger"]),
        f"<ISDEEMEDPOSITIVE>{_dr(amt)}</ISDEEMEDPOSITIVE>",
        f"<ISPARTYLEDGER>{X.yes_no(e['ledger'] == party_ledger)}</ISPARTYLEDGER>",
        f"<AMOUNT>{m(-amt)}</AMOUNT>",
    ]
    for b in e.get("bills") or []:
        parts.append(
            "<BILLALLOCATIONS.LIST>" + X.tag("NAME", b.get("name")) + X.tag("BILLTYPE", b.get("type") or "New Ref")
            + (X.tag("BILLCREDITPERIOD", X.tally_date(b["due_date"])) if b.get("due_date") else "")
            + f"<AMOUNT>{m(-Decimal(b['amount']))}</AMOUNT></BILLALLOCATIONS.LIST>"
        )
    by_cat = defaultdict(list)
    for c in e.get("cost") or []:
        by_cat[c.get("category") or "Primary Cost Category"].append(c)
    for cat, centres in by_cat.items():
        parts.append(
            "<CATEGORYALLOCATIONS.LIST>" + X.tag("CATEGORY", cat) + "<ISDEEMEDPOSITIVE>" + _dr(amt) + "</ISDEEMEDPOSITIVE>"
            + "".join(
                "<COSTCENTREALLOCATIONS.LIST>" + X.tag("NAME", c["centre"])
                + f"<AMOUNT>{m(-Decimal(c['amount']))}</AMOUNT></COSTCENTREALLOCATIONS.LIST>"
                for c in centres
            )
            + "</CATEGORYALLOCATIONS.LIST>"
        )
    for bk in e.get("bank") or []:
        parts.append(
            "<BANKALLOCATIONS.LIST>" + X.tag("TRANSACTIONTYPE", bk.get("transaction_type"))
            + X.tag("INSTRUMENTNUMBER", bk.get("instrument_no"))
            + X.tag("INSTRUMENTDATE", X.tally_date(bk.get("instrument_date")) if bk.get("instrument_date") else None)
            + X.tag("BANKNAME", bk.get("bank_name"))
            + f"<AMOUNT>{m(-Decimal(bk['amount']))}</AMOUNT></BANKALLOCATIONS.LIST>"
        )
    return f"<{list_tag}>{''.join(parts)}</{list_tag}>"


def _inventory_xml(i: dict) -> str:
    qty = Decimal(i["quantity"])
    inward = qty > 0 if qty else i.get("direction") == "in"
    value = abs(Decimal(i.get("amount") or 0))
    signed = -value if inward else value
    unit = i.get("unit") or ""
    qty_txt = f"{q(abs(qty))} {unit}".strip()
    batch = (
        "<BATCHALLOCATIONS.LIST>" + X.tag("GODOWNNAME", i.get("godown") or "Main Location")
        + X.tag("BATCHNAME", i.get("batch") or "Primary Batch")
        + f"<AMOUNT>{m(signed)}</AMOUNT><ACTUALQTY>{X.esc(qty_txt)}</ACTUALQTY>"
        f"<BILLEDQTY>{X.esc(qty_txt)}</BILLEDQTY></BATCHALLOCATIONS.LIST>"
    )
    alloc = ""
    if i.get("ledger"):
        alloc = (
            "<ACCOUNTINGALLOCATIONS.LIST>" + X.tag("LEDGERNAME", i["ledger"])
            + f"<ISDEEMEDPOSITIVE>{X.yes_no(inward)}</ISDEEMEDPOSITIVE><AMOUNT>{m(signed)}</AMOUNT>"
            "</ACCOUNTINGALLOCATIONS.LIST>"
        )
    return (
        "<ALLINVENTORYENTRIES.LIST>" + X.tag("STOCKITEMNAME", i["item"])
        + f"<ISDEEMEDPOSITIVE>{X.yes_no(inward)}</ISDEEMEDPOSITIVE>"
        + (f"<RATE>{X.esc(m(i['rate']))}/{X.esc(unit)}</RATE>" if i.get("rate") else "")
        + (X.tag("DISCOUNT", i.get("discount_pct")) if i.get("discount_pct") else "")
        + f"<AMOUNT>{m(signed)}</AMOUNT><ACTUALQTY>{X.esc(qty_txt)}</ACTUALQTY><BILLEDQTY>{X.esc(qty_txt)}</BILLEDQTY>"
        + batch + alloc + "</ALLINVENTORYENTRIES.LIST>"
    )


def voucher_xml(doc: dict, *, action: str, erp_id: str, ref: TallyRef | None = None) -> str:
    """Build a <VOUCHER> element for Create/Alter/Cancel/Delete."""
    vtype = doc["voucher_type"]
    vdate = X.parse_date(str(doc["date"]))
    if action in ("Cancel", "Delete"):
        return (
            f'<VOUCHER {ref.tag_attrs()} DATE="{X.tally_date(vdate)}" VCHTYPE={X.attr(vtype)} ACTION="{action}">'
            + X.tag("NARRATION", doc.get("narration")) + "</VOUCHER>"
        )

    head = f'<VOUCHER REMOTEID={X.attr(erp_id)} VCHTYPE={X.attr(vtype)} ACTION="Create"'
    if action == "Alter":
        head = f'<VOUCHER {ref.tag_attrs()} VCHTYPE={X.attr(vtype)} ACTION="Alter"'

    inventory = doc.get("inventory") or []
    invoice_mode = bool(inventory)
    view = "Invoice Voucher View" if invoice_mode else "Accounting Voucher View"
    head += f' OBJVIEW="{view}">'

    parts = [
        f"<DATE>{X.tally_date(vdate)}</DATE>",
        X.tag("EFFECTIVEDATE", X.tally_date(X.parse_date(str(doc["effective_date"]))) if doc.get("effective_date") else None),
        X.tag("VOUCHERTYPENAME", vtype),
        X.tag("VOUCHERNUMBER", doc.get("voucher_number")),
        X.tag("REFERENCE", doc.get("reference_no")),
        X.tag("REFERENCEDATE", X.tally_date(X.parse_date(str(doc["reference_date"]))) if doc.get("reference_date") else None),
        X.tag("PARTYLEDGERNAME", doc.get("party_ledger")),
        X.tag("PLACEOFSUPPLY", doc.get("place_of_supply")),
        f"<NARRATION>{X.esc(doc.get('narration') or '')}</NARRATION>",
        f"<PERSISTEDVIEW>{view}</PERSISTEDVIEW>",
        f"<ISINVOICE>{X.yes_no(invoice_mode or bool(doc.get('is_invoice')))}</ISINVOICE>",
    ]
    if doc.get("is_reverse_charge"):
        parts.append("<ISREVERSECHARGEAPPLICABLE>Yes</ISREVERSECHARGEAPPLICABLE>")

    entries = [dict(e) for e in doc.get("entries") or []]
    list_tag = "ALLLEDGERENTRIES.LIST"
    if invoice_mode:
        list_tag = "LEDGERENTRIES.LIST"
        # Ledger amounts carried by inventory accounting allocations must not be repeated as ledger entries.
        alloc = defaultdict(lambda: ZERO)
        for i in inventory:
            if i.get("ledger"):
                value = abs(Decimal(i.get("amount") or 0))
                alloc[i["ledger"]] += value if Decimal(i["quantity"]) > 0 else -value
        remaining = []
        for e in entries:
            amt = Decimal(e["amount"])
            if e["ledger"] in alloc and alloc[e["ledger"]] != ZERO:
                take = alloc[e["ledger"]]
                alloc[e["ledger"]] = ZERO
                amt -= take
                if amt == ZERO:
                    continue
                e["amount"] = amt
            remaining.append(e)
        entries = remaining
        parts.extend(_inventory_xml(i) for i in inventory)

    parts.extend(_ledger_entry_xml(e, list_tag, doc.get("party_ledger")) for e in entries)
    return head + "".join(p for p in parts if p) + "</VOUCHER>"


@dataclass
class TallyVoucher:
    guid: str
    master_id: int | None
    alter_id: int
    remote_id: str | None
    doc: dict
    is_deleted: bool = False
    raw_xml: str | None = None


def _parse_ledger_entry(el: etree._Element) -> dict:
    amt = -X.amount(X.txt(el, "AMOUNT"))
    bills = [
        {"type": X.txt(b, "BILLTYPE") or "New Ref", "name": X.txt(b, "NAME"),
         "amount": m(-X.amount(X.txt(b, "AMOUNT"))), "due_date": None}
        for b in el.findall("BILLALLOCATIONS.LIST") if X.txt(b, "NAME") or X.txt(b, "AMOUNT")
    ]
    cost = []
    for cat in el.findall("CATEGORYALLOCATIONS.LIST"):
        for cc in cat.findall("COSTCENTREALLOCATIONS.LIST"):
            if X.txt(cc, "NAME"):
                cost.append({"category": X.txt(cat, "CATEGORY") or "Primary Cost Category",
                             "centre": X.txt(cc, "NAME"), "amount": m(-X.amount(X.txt(cc, "AMOUNT")))})
    bank = [
        {"transaction_type": X.txt(b, "TRANSACTIONTYPE"), "instrument_no": X.txt(b, "INSTRUMENTNUMBER"),
         "instrument_date": str(X.parse_date(X.txt(b, "INSTRUMENTDATE")) or "") or None,
         "bank_name": X.txt(b, "BANKNAME"), "amount": m(-X.amount(X.txt(b, "AMOUNT")))}
        for b in el.findall("BANKALLOCATIONS.LIST") if X.txt(b, "AMOUNT")
    ]
    return {"ledger": X.clean_name(X.txt(el, "LEDGERNAME")), "amount": m(amt),
            "narration": X.txt(el, "NARRATION"), "bills": bills, "cost": cost, "bank": bank}


def _parse_inventory(el: etree._Element, default_ledger: str | None = None) -> tuple[dict, list[tuple[str, Decimal]]]:
    inward = X.logical(X.txt(el, "ISDEEMEDPOSITIVE"))
    qty, unit = X.quantity(X.txt(el, "ACTUALQTY") or X.txt(el, "BILLEDQTY"))
    value = abs(X.amount(X.txt(el, "AMOUNT")))
    batch = el.find("BATCHALLOCATIONS.LIST")
    allocs = []
    for a in el.findall("ACCOUNTINGALLOCATIONS.LIST"):
        name = X.clean_name(X.txt(a, "LEDGERNAME"))
        if name:
            allocs.append((name, -X.amount(X.txt(a, "AMOUNT"))))
    line = {
        "item": X.clean_name(X.txt(el, "STOCKITEMNAME")),
        "quantity": q(abs(qty) if inward else -abs(qty)),
        "unit": unit,
        "rate": m(X.rate(X.txt(el, "RATE"))),
        "discount_pct": X.txt(el, "DISCOUNT"),
        "amount": m(value),
        "godown": X.txt(batch, "GODOWNNAME"),
        "batch": X.txt(batch, "BATCHNAME"),
        "ledger": allocs[0][0] if allocs else default_ledger,
    }
    if qty == 0:
        line["direction"] = "in" if inward else "out"
    return line, allocs


def parse_voucher(el: etree._Element) -> TallyVoucher:
    vdate = X.parse_date(X.txt(el, "DATE"))
    all_entries = [e for e in el.findall("ALLLEDGERENTRIES.LIST") if X.txt(e, "LEDGERNAME")]
    led_entries = [e for e in el.findall("LEDGERENTRIES.LIST") if X.txt(e, "LEDGERNAME")]
    inv_entries = [e for e in el.findall("ALLINVENTORYENTRIES.LIST") if X.txt(e, "STOCKITEMNAME")]

    entries: list[dict] = []
    inventory: list[dict] = []
    if led_entries or (inv_entries and not all_entries):
        # Invoice view: ledger entries + inventory accounting allocations.
        entries = [_parse_ledger_entry(e) for e in led_entries]
        by_ledger = {e["ledger"]: e for e in entries}
        for inv in inv_entries:
            line, allocs = _parse_inventory(inv)
            inventory.append(line)
            for name, amt in allocs:
                if name in by_ledger:
                    by_ledger[name]["amount"] = m(Decimal(by_ledger[name]["amount"]) + amt)
                else:
                    new = {"ledger": name, "amount": m(amt), "narration": None, "bills": [], "cost": [], "bank": []}
                    entries.append(new)
                    by_ledger[name] = new
    else:
        # Accounting view: inventory (if any) hangs under ledger entries.
        for e in all_entries:
            entry = _parse_ledger_entry(e)
            entries.append(entry)
            for inv in e.findall("INVENTORYALLOCATIONS.LIST"):
                if X.txt(inv, "STOCKITEMNAME"):
                    inventory.append(_parse_inventory(inv, entry["ledger"])[0])

    doc = {
        "voucher_type": X.clean_name(X.txt(el, "VOUCHERTYPENAME") or el.get("VCHTYPE")),
        "voucher_number": X.txt(el, "VOUCHERNUMBER") or "",
        "date": str(vdate) if vdate else None,
        "effective_date": str(X.parse_date(X.txt(el, "EFFECTIVEDATE")) or "") or None,
        "reference_no": X.txt(el, "REFERENCE"),
        "reference_date": str(X.parse_date(X.txt(el, "REFERENCEDATE")) or "") or None,
        "party_ledger": X.clean_name(X.txt(el, "PARTYLEDGERNAME")),
        "narration": X.txt(el, "NARRATION") or "",
        "place_of_supply": X.txt(el, "PLACEOFSUPPLY"),
        "is_invoice": X.logical(X.txt(el, "ISINVOICE")),
        "is_reverse_charge": X.logical(X.txt(el, "ISREVERSECHARGEAPPLICABLE")),
        "is_cancelled": X.logical(X.txt(el, "ISCANCELLED")),
        "statutory": {k: v for k, v in (("irn", X.txt(el, "IRN")), ("eway_bill_no", X.txt(el, "EWAYBILLNO"))) if v},
        "entries": [e for e in entries if Decimal(e["amount"]) != ZERO],
        "inventory": inventory,
    }
    mid = X.txt(el, "MASTERID")
    return TallyVoucher(
        guid=X.txt(el, "GUID") or "",
        master_id=int(mid) if mid else None,
        alter_id=int(X.txt(el, "ALTERID") or 0),
        remote_id=X.txt(el, "REMOTEID") or el.get("REMOTEID"),
        doc=doc,
        is_deleted=X.logical(X.txt(el, "ISDELETED")),
        raw_xml=etree.tostring(el, encoding="unicode"),
    )


# ---------------------------------------------------------------- masters

GROUP_FETCH = ["NAME", "PARENT", "GUID", "MASTERID", "ALTERID", "ISREVENUE", "ISDEEMEDPOSITIVE", "AFFECTSGROSSPROFIT"]
LEDGER_FETCH = [
    "NAME", "PARENT", "GUID", "MASTERID", "ALTERID", "OPENINGBALANCE", "CLOSINGBALANCE", "ISBILLWISEON",
    "BILLCREDITPERIOD", "PARTYGSTIN", "INCOMETAXNUMBER", "LEDSTATENAME", "GSTREGISTRATIONTYPE", "TAXTYPE",
    "GSTDUTYHEAD", "MAILINGNAME", "BANKDETAILS", "IFSCODE", "BRANCHNAME", "BANKACCHOLDERNAME",
    "ADDRESS", "LEDMAILINGDETAILS", "PINCODE", "COUNTRYNAME", "LEDGSTREGDETAILS",
    "LEDGERCONTACT", "LEDGERPHONE", "LEDGERMOBILE", "LEDGERFAX", "EMAIL", "EMAILCC", "WEBSITE", "CONTACTDETAILS",
    "PAYMENTDETAILS", "MSMEREGISTRATIONDETAILS",
]
VOUCHER_TYPE_FETCH = ["NAME", "PARENT", "GUID", "MASTERID", "ALTERID", "NUMBERINGMETHOD", "ISACTIVE"]
STOCK_ITEM_FETCH = ["NAME", "PARENT", "GUID", "MASTERID", "ALTERID", "BASEUNITS", "ADDITIONALUNITS",
                    "DENOMINATOR", "CONVERSION", "CATEGORY", "MAILINGNAME", "OPENINGBALANCE", "OPENINGRATE",
                    "OPENINGVALUE", "BATCHALLOCATIONS", "CLOSINGBALANCE", "CLOSINGRATE", "CLOSINGVALUE",
                    "GSTAPPLICABLE", "GSTTYPEOFSUPPLY", "GSTDETAILS", "HSNDETAILS", "HSNCODE",
                    "STANDARDCOSTLIST", "STANDARDPRICELIST", "COSTINGMETHOD", "VALUATIONMETHOD", "ISBATCHWISEON",
                    "HASMFGDATE", "ISPERISHABLEON", "IGNORENEGATIVESTOCK", "REORDERBASE", "ENTEREDBY"]
UNIT_FETCH = ["NAME", "GUID", "MASTERID", "ALTERID", "ORIGINALNAME", "DECIMALPLACES", "ISSIMPLEUNIT",
              "BASEUNITS", "ADDITIONALUNITS", "CONVERSION"]
GODOWN_FETCH = ["NAME", "PARENT", "GUID", "MASTERID", "ALTERID"]
STOCK_GROUP_FETCH = ["NAME", "PARENT", "GUID", "MASTERID", "ALTERID", "GSTDETAILS", "HSNDETAILS"]
COST_CENTRE_FETCH = ["NAME", "PARENT", "CATEGORY", "GUID", "MASTERID", "ALTERID"]


def group_nature(is_revenue: bool, is_deemed_positive: bool) -> str:
    if is_revenue:
        return "expense" if is_deemed_positive else "income"
    return "asset" if is_deemed_positive else "liability"


def parse_master(el: etree._Element) -> dict:
    """Generic master record: name, parent, ids and every simple child tag."""
    out = {
        "name": X.clean_name(el.get("NAME") or X.txt(el, "NAME")),
        "reserved_name": X.clean_name(el.get("RESERVEDNAME")),
        "parent": X.clean_name(X.txt(el, "PARENT")),
        "guid": X.txt(el, "GUID"),
        "master_id": int(X.txt(el, "MASTERID") or 0) or None,
        "alter_id": int(X.txt(el, "ALTERID") or 0),
    }
    if out["parent"] in (X.PRIMARY, ""):
        out["parent"] = None
    for child in el:
        if isinstance(child.tag, str) and not len(child) and child.tag not in ("NAME", "PARENT", "GUID", "MASTERID", "ALTERID"):
            out[child.tag.lower()] = (child.text or "").strip()
    if el.tag == "LEDGER":
        out["details"] = ledger_details(el)
    elif el.tag == "STOCKITEM":
        out["details"] = stock_item_details(el)
    elif el.tag == "STOCKGROUP":
        out["details"] = gst_hsn(el)
    return out


def gst_hsn(el: etree._Element) -> dict:
    """HSN and GST rate of a stock item or group, from the dated lists that apply today.

    `inherited` is True when Tally says "As per Company/Stock Group": the values then come from the group.
    """
    gst = _latest(el, "GSTDETAILS.LIST", "APPLICABLEFROM")
    hsn = _latest(el, "HSNDETAILS.LIST", "APPLICABLEFROM")
    rates = {}
    for r in (gst.iter("RATEDETAILS.LIST") if gst is not None else []):
        head, value = X.txt(r, "GSTRATEDUTYHEAD"), X.txt(r, "GSTRATE")
        if head and value is not None:
            rates[head] = float(X.amount(value))
    total = rates.get("IGST") or (rates.get("CGST", 0) + rates.get("SGST/UTGST", rates.get("SGST", 0))) or None
    source = X.txt(gst, "SRCOFGSTDETAILS") or X.txt(hsn, "SRCOFHSNDETAILS") or ""
    return {
        "hsn_code": X.txt(hsn, "HSNCODE") or X.txt(el, "HSNCODE"),
        "hsn_description": X.txt(hsn, "HSN") or X.txt(hsn, "HSNCLASSIFICATIONNAME") or X.txt(gst, "HSNMASTERNAME"),
        "gst_rate": total if any(rates.values()) else None,
        "gst_rates": {k: v for k, v in rates.items() if v} or None,
        "taxability": X.txt(gst, "TAXABILITY"),
        "inherited": source.startswith("As per"),
        "itc_eligible": None if X.txt(gst, "GSTINELIGIBLEITC") is None else X.logical(X.txt(gst, "GSTINELIGIBLEITC")),
    }


def _latest_rate(el: etree._Element, list_tag: str) -> Decimal | None:
    entry = _latest(el, list_tag, "DATE")
    return X.rate(X.txt(entry, "RATE")) if entry is not None and X.txt(entry, "RATE") else None


def stock_item_details(el: etree._Element) -> dict:
    """Everything a stock item carries in Tally beyond its name, group and opening (checked live, Nov 2026)."""
    closing_qty, _ = X.quantity(X.txt(el, "CLOSINGBALANCE"))
    openings = []
    for b in el.findall("BATCHALLOCATIONS.LIST"):
        qty, _ = X.quantity(X.txt(b, "OPENINGBALANCE"))
        if qty or X.txt(b, "OPENINGVALUE"):
            openings.append({"godown": X.txt(b, "GODOWNNAME"), "batch": X.txt(b, "BATCHNAME"), "qty": str(qty),
                             "rate": str(X.rate(X.txt(b, "OPENINGRATE"))),
                             "value": str(abs(X.amount(X.txt(b, "OPENINGVALUE"))))})
    gst = gst_hsn(el)
    return {
        "alias": X.txt(el.find("MAILINGNAME.LIST"), "MAILINGNAME") if el.find("MAILINGNAME.LIST") is not None else None,
        "category": None if X.txt(el, "CATEGORY") in (None, "Not Applicable") else X.txt(el, "CATEGORY"),
        "conversion": X.amount(X.txt(el, "CONVERSION")) or None,
        "denominator": X.amount(X.txt(el, "DENOMINATOR")) or None,
        "gst_applicable": X.txt(el, "GSTAPPLICABLE"),
        "supply_type": X.txt(el, "GSTTYPEOFSUPPLY"),
        **{k: gst[k] for k in ("hsn_code", "hsn_description", "gst_rate", "gst_rates", "taxability", "itc_eligible")},
        "gst_inherited": gst["inherited"],
        "valuation_method": X.txt(el, "COSTINGMETHOD"),
        "market_valuation": X.txt(el, "VALUATIONMETHOD"),
        "batch_wise": X.logical(X.txt(el, "ISBATCHWISEON")),
        "has_mfg_date": X.logical(X.txt(el, "HASMFGDATE")),
        "perishable": X.logical(X.txt(el, "ISPERISHABLEON")),
        "ignore_negative_stock": X.logical(X.txt(el, "IGNORENEGATIVESTOCK")),
        "reorder_level": X.quantity(X.txt(el, "REORDERBASE"))[0] or None,
        "standard_cost": _latest_rate(el, "STANDARDCOSTLIST.LIST"),
        "standard_price": _latest_rate(el, "STANDARDPRICELIST.LIST"),
        "closing_qty": closing_qty if X.txt(el, "CLOSINGBALANCE") else None,
        "closing_rate": X.rate(X.txt(el, "CLOSINGRATE")) if X.txt(el, "CLOSINGRATE") else None,
        "closing_value": -X.amount(X.txt(el, "CLOSINGVALUE")) if X.txt(el, "CLOSINGVALUE") else None,
        "opening_allocations": openings or None,
        "entered_by": X.txt(el, "ENTEREDBY"),
    }


def _latest(el: etree._Element, list_tag: str, date_tag: str) -> etree._Element | None:
    """The entry of a dated list (GST registration, mailing details, MSME) that applies today."""
    entries = [e for e in el.findall(list_tag) if len(e)]
    return max(entries, key=lambda e: X.txt(e, date_tag) or "", default=None)


def _lines(el: etree._Element | None) -> str | None:
    if el is None:
        return None
    lines = [(a.text or "").strip() for a in el.findall("ADDRESS.LIST/ADDRESS")]
    return "\n".join(line for line in lines if line) or None


def ledger_details(el: etree._Element) -> dict:
    """Party details a ledger carries in Tally: address, GST, contacts, bank accounts and MSME.

    Field names as exported by TallyPrime (checked against a live 4k-ledger company, Nov 2026). Dated
    lists take the latest entry; flat fields are the fallback for older releases.
    """
    mail = _latest(el, "LEDMAILINGDETAILS.LIST", "APPLICABLEFROM")
    gst = _latest(el, "LEDGSTREGDETAILS.LIST", "APPLICABLEFROM")
    msme = _latest(el, "MSMEREGISTRATIONDETAILS.LIST", "FROMDATE")
    contacts = [{"label": X.txt(c, "NAME"), "isd": X.txt(c, "COUNTRYISDCODE"), "number": X.txt(c, "PHONENUMBER"),
                 "whatsapp": X.logical(X.txt(c, "ISDEFAULTWHATSAPPNUM"))}
                for c in el.findall("CONTACTDETAILS.LIST") if X.txt(c, "PHONENUMBER")]
    banks = [{"label": X.txt(b, "TRANSACTIONNAME"), "bank_name": X.txt(b, "BANKNAME"),
              "account_no": X.txt(b, "ACCOUNTNUMBER"), "ifsc": X.txt(b, "IFSCODE"),
              "favouring": X.txt(b, "PAYMENTFAVOURING"), "transfer_type": X.txt(b, "DEFAULTTRANSACTIONTYPE"),
              "default": X.logical(X.txt(b, "SETASDEFAULT"))}
             for b in el.findall("PAYMENTDETAILS.LIST") if X.txt(b, "ACCOUNTNUMBER") or X.txt(b, "BANKNAME")]

    def pick(node, tag, flat=None):
        return X.txt(node, tag) or (X.txt(el, flat) if flat else None)

    return {
        "address": _lines(mail) or _lines(el),
        "mailing_name": pick(mail, "MAILINGNAME", "MAILINGNAME"),
        "pincode": pick(mail, "PINCODE", "PINCODE"),
        "state": pick(mail, "STATE", "LEDSTATENAME"),
        "country": pick(mail, "COUNTRY", "COUNTRYNAME"),
        "gstin": pick(gst, "GSTIN", "PARTYGSTIN"),
        "gst_registration_type": pick(gst, "GSTREGISTRATIONTYPE", "GSTREGISTRATIONTYPE"),
        "place_of_supply": pick(gst, "PLACEOFSUPPLY"),
        "contact_person": X.txt(el, "LEDGERCONTACT"),
        "phone": X.txt(el, "LEDGERPHONE"),
        "mobile": X.txt(el, "LEDGERMOBILE") or next((c["number"] for c in contacts), None),
        "fax": X.txt(el, "LEDGERFAX"),
        "email": X.txt(el, "EMAIL"),
        "email_cc": X.txt(el, "EMAILCC"),
        "website": X.txt(el, "WEBSITE"),
        "contacts": contacts or None,
        "bank_account_no": X.txt(el, "BANKDETAILS"),
        "bank_ifsc": X.txt(el, "IFSCODE"),
        "bank_branch": X.txt(el, "BRANCHNAME"),
        "bank_account_holder": X.txt(el, "BANKACCHOLDERNAME"),
        "party_banks": banks or None,
        "msme_type": pick(msme, "ENTERPRISETYPE"),
        "msme_udyam_no": pick(msme, "UDYAMREGNUMBER"),
        "msme_activity": pick(msme, "MSMEACTIVITYTYPE"),
        "msme_from": X.parse_date(pick(msme, "FROMDATE")),
    }


def _address_list(address: str | None) -> str:
    lines = [line.strip() for line in (address or "").splitlines() if line.strip()]
    return ('<ADDRESS.LIST TYPE="String">' + "".join(X.tag("ADDRESS", line) for line in lines) + "</ADDRESS.LIST>"
            if lines else "")


def ledger_xml(ledger: dict, *, action: str, ref: TallyRef | None = None) -> str:
    """A ledger as Tally imports it: the same tags and dated lists Tally exports (see ledger_details)."""
    head = f'<LEDGER NAME={X.attr(ledger["name"])} ACTION="Create">'
    if action == "Alter":
        head = f'<LEDGER NAME={X.attr(ledger.get("old_name") or ledger["name"])} ACTION="Alter">'
    ob = Decimal(ledger.get("opening_balance") or 0)
    since = X.tally_date(ledger.get("applicable_from")) if ledger.get("applicable_from") else None
    get = ledger.get
    mailing = (
        "<LEDMAILINGDETAILS.LIST>" + X.tag("APPLICABLEFROM", since) + X.tag("MAILINGNAME", get("mailing_name"))
        + _address_list(get("address")) + X.tag("STATE", get("state")) + X.tag("COUNTRY", get("country"))
        + X.tag("PINCODE", get("pincode")) + "</LEDMAILINGDETAILS.LIST>"
    ) if since and any(get(f) for f in ("mailing_name", "address", "state", "country", "pincode")) else ""
    gst = (
        "<LEDGSTREGDETAILS.LIST>" + X.tag("APPLICABLEFROM", since)
        + X.tag("GSTREGISTRATIONTYPE", get("gst_registration_type")) + X.tag("STATE", get("state"))
        + X.tag("PLACEOFSUPPLY", get("place_of_supply")) + X.tag("GSTIN", get("gstin")) + "</LEDGSTREGDETAILS.LIST>"
    ) if since and (get("gstin") or get("gst_registration_type")) else ""
    banks = "".join(
        "<PAYMENTDETAILS.LIST>" + X.tag("TRANSACTIONNAME", b.get("label") or "Primary")
        + X.tag("BANKNAME", b.get("bank_name")) + X.tag("ACCOUNTNUMBER", b.get("account_no"))
        + X.tag("IFSCODE", b.get("ifsc")) + X.tag("PAYMENTFAVOURING", b.get("favouring"))
        + X.tag("DEFAULTTRANSACTIONTYPE", b.get("transfer_type")) + f"<SETASDEFAULT>{X.yes_no(bool(b.get('default')))}</SETASDEFAULT>"
        + "</PAYMENTDETAILS.LIST>"
        for b in get("party_banks") or [])
    contacts = "".join(
        "<CONTACTDETAILS.LIST>" + X.tag("NAME", c.get("label") or "Primary Mobile No.") + X.tag("COUNTRYISDCODE", c.get("isd"))
        + X.tag("PHONENUMBER", c.get("number")) + f"<ISDEFAULTWHATSAPPNUM>{X.yes_no(bool(c.get('whatsapp')))}</ISDEFAULTWHATSAPPNUM>"
        + "</CONTACTDETAILS.LIST>"
        for c in get("contacts") or [] if c.get("number"))
    msme = (
        "<MSMEREGISTRATIONDETAILS.LIST>" + X.tag("FROMDATE", X.tally_date(get("msme_from")) if get("msme_from") else since)
        + X.tag("ENTERPRISETYPE", get("msme_type")) + X.tag("MSMEACTIVITYTYPE", get("msme_activity"))
        + X.tag("UDYAMREGNUMBER", get("msme_udyam_no")) + "</MSMEREGISTRATIONDETAILS.LIST>"
    ) if get("msme_type") or get("msme_udyam_no") else ""
    return (
        head
        + f'<NAME.LIST TYPE="String"><NAME>{X.esc(ledger["name"])}</NAME></NAME.LIST>'
        + X.tag("PARENT", ledger["group"])
        + f"<ISBILLWISEON>{X.yes_no(bool(get('is_billwise')))}</ISBILLWISEON>"
        + (X.tag("BILLCREDITPERIOD", f"{get('credit_days')} Days") if get("credit_days") else "")
        + f"<OPENINGBALANCE>{m(-ob)}</OPENINGBALANCE>"
        + X.tag("INCOMETAXNUMBER", get("pan"))
        # flat fields (older releases read these) and the dated lists (TallyPrime 3+)
        + X.tag("MAILINGNAME", get("mailing_name")) + _address_list(get("address"))
        + X.tag("LEDSTATENAME", get("state")) + X.tag("COUNTRYNAME", get("country")) + X.tag("PINCODE", get("pincode"))
        + X.tag("PARTYGSTIN", get("gstin")) + X.tag("GSTREGISTRATIONTYPE", get("gst_registration_type"))
        + mailing + gst
        + X.tag("LEDGERCONTACT", get("contact_person")) + X.tag("LEDGERPHONE", get("phone"))
        + X.tag("LEDGERMOBILE", get("mobile")) + X.tag("LEDGERFAX", get("fax")) + X.tag("EMAIL", get("email"))
        + X.tag("EMAILCC", get("email_cc")) + X.tag("WEBSITE", get("website")) + contacts
        + X.tag("BANKDETAILS", get("bank_account_no")) + X.tag("IFSCODE", get("bank_ifsc"))
        + X.tag("BRANCHNAME", get("bank_branch")) + X.tag("BANKACCHOLDERNAME", get("bank_account_holder"))
        + banks + msme
        + "</LEDGER>"
    )
