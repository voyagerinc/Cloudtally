"""Connector codec tests. Fixtures copy the structure of real TallyPrime exports (values are synthetic)."""
from decimal import Decimal

from lxml import etree

from app.connectors.tally import codec
from app.connectors.tally import xmlutil as X
from app.connectors.tally.client import _records
from app.connectors.tally.errors import classify_tally_message
from app.services.canonical import content_hash

LEDGER_EXPORT = """<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DESC>
<CMPINFO><COMPANY>0</COMPANY><GROUP>0</GROUP><LEDGER>0</LEDGER><VOUCHER>0</VOUCHER></CMPINFO></DESC>
<DATA><COLLECTION>
 <LEDGER NAME="3A Associates" RESERVEDNAME="">
  <GUID TYPE="String">bbe77eb1-0000-4b74-a4c4-06e292f764cd-00004b41</GUID>
  <PARENT TYPE="String">Sundry Debtor</PARENT>
  <PARTYGSTIN TYPE="String">24AAAAA0000A1Z5</PARTYGSTIN>
  <ISBILLWISEON TYPE="Logical">Yes</ISBILLWISEON>
  <ALTERID TYPE="Number"> 169981</ALTERID>
  <MASTERID TYPE="Number"> 19265</MASTERID>
  <OPENINGBALANCE TYPE="Amount">-1,250.50</OPENINGBALANCE>
 </LEDGER>
 <GROUP NAME="Capital Account" RESERVEDNAME="Capital Account">
  <GUID TYPE="String">bbe77eb1-0000-4b74-a4c4-06e292f764cd-00000001</GUID>
  <PARENT TYPE="String">&#4; Primary</PARENT>
  <ISREVENUE TYPE="Logical">No</ISREVENUE><ISDEEMEDPOSITIVE TYPE="Logical">No</ISDEEMEDPOSITIVE>
  <ALTERID TYPE="Number"> 167823</ALTERID><MASTERID TYPE="Number"> 1</MASTERID>
 </GROUP>
</COLLECTION></DATA></BODY></ENVELOPE>"""


def test_masters_parse_and_cmpinfo_is_ignored():
    root = X.parse(LEDGER_EXPORT)
    ledgers = [codec.parse_master(e) for e in _records(root, "LEDGER")]
    assert len(ledgers) == 1
    led = ledgers[0]
    assert led["name"] == "3A Associates" and led["master_id"] == 19265 and led["alter_id"] == 169981
    assert led["guid"].endswith("-00004b41") and int("4b41", 16) == 19265   # GUID = company GUID + hex MasterID
    assert -X.amount(led["openingbalance"]) == Decimal("1250.50")           # Tally negative = debit
    group = codec.parse_master(next(_records(root, "GROUP")))
    assert group["parent"] is None and group["reserved_name"] == "Capital Account"
    assert codec.group_nature(False, False) == "liability"


def test_amount_and_quantity_formats():
    assert X.amount(" -1,234.50") == Decimal("-1234.50")
    assert X.amount("1234.50 Dr") == Decimal("-1234.50")
    assert X.amount("$12.00 @ ₹ 83.10/$ = ₹ 997.20") == Decimal("997.20")
    assert X.quantity(" 12.5 Nos") == (Decimal("12.5"), "Nos")
    assert X.rate("50.00/Nos") == Decimal("50.00")
    assert str(X.parse_date("20260401")) == "2026-04-01" and str(X.parse_date("1-Apr-2026")) == "2026-04-01"


def _round_trip(doc: dict) -> dict:
    xml = codec.voucher_xml(doc, action="Create", erp_id="7b1c")
    el = etree.fromstring(xml)
    return codec.parse_voucher(el).doc


def test_accounting_voucher_round_trip_keeps_hash():
    doc = {"voucher_type": "Receipt", "voucher_number": "R-9", "date": "2026-05-01", "party_ledger": "Acme & Sons",
           "narration": "Part payment <cheque>", "entries": [
               {"ledger": "Acme & Sons", "amount": "-5000.00",
                "bills": [{"type": "Agst Ref", "name": "INV-1", "amount": "-5000.00"}],
                "cost": [], "bank": []},
               {"ledger": "HDFC Bank", "amount": "5000.00", "bills": [],
                "cost": [{"category": "Primary Cost Category", "centre": "Ahmedabad", "amount": "5000.00"}],
                "bank": [{"transaction_type": "Cheque", "instrument_no": "000123", "amount": "5000.00"}]}]}
    back = _round_trip(doc)
    assert content_hash(back) == content_hash(doc)
    assert back["narration"] == "Part payment <cheque>"
    assert back["entries"][1]["bank"][0]["instrument_no"] == "000123"


def test_invoice_round_trip_moves_sales_into_inventory_allocation():
    doc = {"voucher_type": "Sales", "voucher_number": "S-1", "date": "2026-05-02", "party_ledger": "Acme",
           "entries": [{"ledger": "Acme", "amount": "1180.00"}, {"ledger": "Sales", "amount": "-1000.00"},
                       {"ledger": "Output IGST", "amount": "-180.00"}],
           "inventory": [{"item": "Spool", "quantity": "-4", "unit": "Nos", "rate": "250", "amount": "1000", "ledger": "Sales"}]}
    xml = codec.voucher_xml(doc, action="Create", erp_id="x")
    el = etree.fromstring(xml)
    assert [e.findtext("LEDGERNAME") for e in el.findall("LEDGERENTRIES.LIST")] == ["Acme", "Output IGST"]
    assert el.findtext("ALLINVENTORYENTRIES.LIST/ACCOUNTINGALLOCATIONS.LIST/AMOUNT") == "1000.00"
    assert content_hash(codec.parse_voucher(el).doc) == content_hash(doc)


def test_alter_and_cancel_use_stable_identity():
    doc = {"voucher_type": "Payment", "date": "2026-05-01", "entries": []}
    ref = codec.TallyRef(master_id=42, guid="g-1")
    assert 'TAGNAME="MASTERID" TAGVALUE="42"' in codec.voucher_xml(doc, action="Cancel", erp_id="e", ref=ref)
    ref.alter_by = "guid"
    assert 'TAGNAME="GUID" TAGVALUE="g-1"' in codec.voucher_xml(doc, action="Alter", erp_id="e", ref=ref)


def test_import_response_and_error_classification():
    ok = codec.parse_import_response("<ENVELOPE><BODY><DATA><IMPORTRESULT><CREATED>1</CREATED><ERRORS>0</ERRORS>"
                                     "<LASTVCHID>77</LASTVCHID></IMPORTRESULT></DATA></BODY></ENVELOPE>")
    assert ok.ok and ok.created == 1 and ok.last_vch_id == 77
    bad = codec.parse_import_response("<RESPONSE><LINEERROR>Ledger 'Ghost' does not exist!</LINEERROR>"
                                      "<CREATED>0</CREATED><ERRORS>1</ERRORS></RESPONSE>")
    assert not bad.ok and classify_tally_message(bad.line_errors[0]) == "missing_master"
    assert classify_tally_message("Voucher date is out of period") == "period_locked"
    assert classify_tally_message("Something odd") == "tally_rejection"
