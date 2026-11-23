"""Custom (TDL) fields of a customised Tally: e.g. chassis no. and motor no. of each vehicle sold."""
from lxml import etree

from app.connectors.tally import codec
from app.connectors.tally import xmlutil as X
from app.services.canonical import content_hash
from app.services.sync import custom_fields as cf

# Made-up sale of two e-rickshaws in the shape Tally exports a customised voucher: a voucher-level field,
# chassis / motor no. on each batch, a date field, and one field whose TDL is not loaded (UDF:_UDF_ number).
SALE = """<ENVELOPE><VOUCHER VCHTYPE="Sales">
 <DATE>20261120</DATE><VOUCHERTYPENAME>Sales</VOUCHERTYPENAME><VOUCHERNUMBER>S-17</VOUCHERNUMBER>
 <MASTERID>901</MASTERID><ALTERID>5</ALTERID><GUID>g-901</GUID><PARTYLEDGERNAME>Ram Motors</PARTYLEDGERNAME>
 <DEALERCODE.LIST DESC="`Dealer Code`" ISLIST="YES" TYPE="String" INDEX="1003"><DEALERCODE DESC="`Dealer Code`">D-42</DEALERCODE></DEALERCODE.LIST>
 <UDF:_UDF_788529207.LIST DESC="" ISLIST="YES" TYPE="String" INDEX="1010"><UDF:_UDF_788529207 DESC="">X1</UDF:_UDF_788529207></UDF:_UDF_788529207.LIST>
 <LEDGERENTRIES.LIST><LEDGERNAME>Ram Motors</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>-200000</AMOUNT></LEDGERENTRIES.LIST>
 <ALLINVENTORYENTRIES.LIST>
  <STOCKITEMNAME>E-Rickshaw L3</STOCKITEMNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
  <RATE>100000/Nos</RATE><AMOUNT>200000</AMOUNT><ACTUALQTY> 2 Nos</ACTUALQTY><BILLEDQTY> 2 Nos</BILLEDQTY>
  <WARRANTYUPTO.LIST DESC="`Warranty upto`" ISLIST="YES" TYPE="Date" INDEX="1005"><WARRANTYUPTO DESC="`Warranty upto`">20271120</WARRANTYUPTO></WARRANTYUPTO.LIST>
  <BATCHALLOCATIONS.LIST><GODOWNNAME>Main Location</GODOWNNAME><BATCHNAME>B1</BATCHNAME><BILLEDQTY> 1 Nos</BILLEDQTY>
   <CHASSISNO.LIST DESC="`Chassis No`" ISLIST="YES" TYPE="String" INDEX="1001"><CHASSISNO DESC="`Chassis No`">MD9AF1001</CHASSISNO></CHASSISNO.LIST>
   <MOTORNO.LIST DESC="`Motor No`" ISLIST="YES" TYPE="String" INDEX="1002"><MOTORNO DESC="`Motor No`">MT-5001</MOTORNO></MOTORNO.LIST>
  </BATCHALLOCATIONS.LIST>
  <BATCHALLOCATIONS.LIST><GODOWNNAME>Main Location</GODOWNNAME><BATCHNAME>B2</BATCHNAME><BILLEDQTY> 1 Nos</BILLEDQTY>
   <CHASSISNO.LIST DESC="`Chassis No`" ISLIST="YES" TYPE="String" INDEX="1001"><CHASSISNO DESC="`Chassis No`">MD9AF1002</CHASSISNO></CHASSISNO.LIST>
   <MOTORNO.LIST DESC="`Motor No`" ISLIST="YES" TYPE="String" INDEX="1002"><MOTORNO DESC="`Motor No`">MT-5002</MOTORNO></MOTORNO.LIST>
  </BATCHALLOCATIONS.LIST>
  <ACCOUNTINGALLOCATIONS.LIST><LEDGERNAME>Sales</LEDGERNAME><AMOUNT>200000</AMOUNT></ACCOUNTINGALLOCATIONS.LIST>
 </ALLINVENTORYENTRIES.LIST>
</VOUCHER></ENVELOPE>"""


def _voucher():
    return X.parse(SALE).find("VOUCHER")


def test_custom_fields_are_read_from_voucher_items_and_batches():
    got = codec.custom_fields_of(_voucher())
    assert got["voucher"] == {"Dealer Code": "D-42", "UDF:_UDF_788529207": "X1"}
    assert got["items"] == [
        {"item": "E-Rickshaw L3", "batch": "B1", "quantity": "1 Nos",
         "fields": {"Warranty upto": "2027-11-20", "Chassis No": "MD9AF1001", "Motor No": "MT-5001"}},
        {"item": "E-Rickshaw L3", "batch": "B2", "quantity": "1 Nos",
         "fields": {"Warranty upto": "2027-11-20", "Chassis No": "MD9AF1002", "Motor No": "MT-5002"}}]
    # Kept with the voucher, but not accounting content: the content hash does not change.
    doc = codec.parse_voucher(_voucher()).doc
    assert doc["custom_fields"] == got
    assert content_hash(doc) == content_hash({**doc, "custom_fields": {}})
    # A voucher without any customisation has none.
    plain = X.parse(SALE.replace("INDEX=", "X=").replace("UDF:_UDF_788529207", "OTHER")).find("VOUCHER")
    assert codec.custom_fields_of(plain) == {}


def test_discovery_lists_each_field_with_its_level_and_fetch_name():
    fields = {(f["level"], f["label"]): f for f in cf._fields_in(_voucher())}
    assert set(fields) == {("voucher", "Dealer Code"), ("voucher", "UDF:_UDF_788529207"),
                           ("item", "Warranty upto"), ("batch", "Chassis No"), ("batch", "Motor No")}
    assert fields[("batch", "Chassis No")]["tag"] == "CHASSISNO"
    assert codec.fetch_name("CHASSISNO") == "CHASSISNO"
    assert codec.fetch_name("UDF:_UDF_788529207") is None          # an unnamed field cannot be asked for by name


def test_day_book_request_is_one_day():
    xml = codec.export_day_book("Demo Motors & Co", __import__("datetime").date(2026, 11, 20))
    root = etree.fromstring(xml.encode())
    assert root.findtext(".//ID") == "Day Book" and root.findtext(".//SVFROMDATE") == "20261120"
    assert root.findtext(".//SVTODATE") == "20261120" and root.findtext(".//SVCURRENTCOMPANY") == "Demo Motors & Co"


def test_sync_brings_custom_fields_and_the_report_lists_them(db, company, tc, mock):
    """End to end against the mock Tally: sync keeps the fields, discovery finds them, the report shows them."""
    from sqlalchemy import select
    from app.models import TallyConnector, TallyMapping, Voucher
    from app.services.sync import common
    from app.services.sync.initial_import import import_masters
    from app.core.db import SessionLocal
    from app.workers import sync_worker
    from tests.conftest import USER
    from datetime import date
    tc.add_item("E-Rickshaw L3")
    tc.add_ledger("Ram Motors", "Sundry Debtors")
    conn = db.get(TallyConnector, company.id)
    with common.client_for(conn) as client:
        import_masters(db, company, conn, client, USER)
    xml = SALE.split("<ENVELOPE>")[1].split("</ENVELOPE>")[0].replace("<DATE>20261120", "<DATE>20260420")
    udf = xml[xml.index("<UDF:"):xml.index("</UDF:_UDF_788529207.LIST>") + len("</UDF:_UDF_788529207.LIST>")]
    xml = xml.replace(udf, "")              # the mock's strict parser has no UDF namespace (codec reads it, above)
    for tag in ("MASTERID", "ALTERID", "GUID"):
        xml = xml.replace(xml[xml.index(f"<{tag}>"):xml.index(f"</{tag}>") + len(tag) + 3], "")
    mid = mock.enter_voucher(tc, xml)
    sync_worker.run_once(SessionLocal)
    vid = db.scalar(select(TallyMapping.erp_id).where(TallyMapping.tally_master_id == mid, TallyMapping.entity_type == "voucher"))
    v = db.get(Voucher, vid)
    assert [i["fields"]["Chassis No"] for i in v.custom_fields["items"]] == ["MD9AF1001", "MD9AF1002"]

    found = cf.discover(db, company.id, date(2026, 4, 20))
    labels = {f["label"]: f for f in found["fields"]}
    assert {"Chassis No", "Motor No", "Dealer Code"} <= set(labels) and labels["Chassis No"]["comes_with_sync"] is True
    cf.save(db, company.id, [{"tag": "CHASSISNO", "label": "Chassis No", "level": "batch"},
                             {"tag": "MOTORNO", "label": "Motor No", "level": "batch"}])
    assert common.custom_fetch(db.get(TallyConnector, company.id)) == ["CHASSISNO", "MOTORNO"]

    rep = cf.register(db, company.id, date(2026, 4, 1), date(2027, 3, 31))
    assert rep["fields"][:2] == ["Chassis No", "Motor No"]                   # the chosen order first
    assert [(r["batch"], r["Chassis No"], r["Motor No"], r["Dealer Code"]) for r in rep["rows"]] == [
        ("B1", "MD9AF1001", "MT-5001", "D-42"), ("B2", "MD9AF1002", "MT-5002", "D-42")]

    v.custom_fields = None                                                    # e.g. synced before this release
    db.commit()
    r = cf.refresh(db, company.id, date(2026, 4, 1), date(2027, 3, 31))
    db.refresh(v)
    assert r["updated"] >= 1 and v.custom_fields["items"][1]["fields"]["Motor No"] == "MT-5002"


def test_fields_not_brought_by_sync_are_read_from_the_day_book_after_each_sync(db, company, tc, mock):
    from datetime import date
    from sqlalchemy import select
    from app.core.db import SessionLocal
    from app.models import TallyMapping, Voucher
    from app.models.base import utcnow
    from app.workers import sync_worker
    tc.add_item("E-Rickshaw L3")
    tc.add_ledger("Ram Motors", "Sundry Debtors")
    from app.services.sync import common
    from app.services.sync.initial_import import import_masters
    from app.models import TallyConnector
    from tests.conftest import USER
    conn = db.get(TallyConnector, company.id)
    with common.client_for(conn) as client:
        import_masters(db, company, conn, client, USER)
    cf.save(db, company.id, [{"tag": "CHASSISNO", "label": "Chassis No", "level": "batch",
                              "voucher_types": ["Sales"], "comes_with_sync": False}])
    xml = SALE.split("<ENVELOPE>")[1].split("</ENVELOPE>")[0].replace("<DATE>20261120", "<DATE>20260421")
    udf = xml[xml.index("<UDF:"):xml.index("</UDF:_UDF_788529207.LIST>") + len("</UDF:_UDF_788529207.LIST>")]
    xml = xml.replace(udf, "")
    for tag in ("MASTERID", "ALTERID", "GUID"):
        xml = xml.replace(xml[xml.index(f"<{tag}>"):xml.index(f"</{tag}>") + len(tag) + 3], "")
    mid = mock.enter_voucher(tc, xml)
    before = sum(1 for r in mock.requests if "<ID>Day Book</ID>" in r)
    stats = sync_worker.run_once(SessionLocal)
    assert sum(1 for r in mock.requests if "<ID>Day Book</ID>" in r) == before + 1      # just that one day
    vid = db.scalar(select(TallyMapping.erp_id).where(TallyMapping.tally_master_id == mid))
    v = db.get(Voucher, vid)
    assert v.custom_fields["items"][0]["fields"]["Chassis No"] == "MD9AF1001"
    # From the books' beginning, in the background, with progress.
    v.custom_fields = None
    db.commit()
    job = cf.start_refresh(SessionLocal, company.id, company.books_from, date(2027, 3, 31))
    import time
    for _ in range(100):
        if not cf.job(company.id)["running"]:
            break
        time.sleep(0.1)
    j = cf.job(company.id)
    assert j["error"] is None and j["days"] == j["days_done"] >= 1 and j["updated"] == 1, j
    db.refresh(v)
    assert v.custom_fields["items"][1]["fields"]["Motor No"] == "MT-5002"
