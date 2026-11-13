"""Bidirectional sync acceptance tests (PRD §6.1 scenarios, §12 test groups) against the mock Tally."""
from datetime import date
from decimal import Decimal

import pytest

from sqlalchemy import func, select

from app.models import (
    Ledger, OutboxStatus, StockGroup, StockItem, SyncConflict, SyncInbox, SyncOutbox, SyncProgress, SyncStatus, TallyConnector,
    TallyMapping, Voucher,
    VoucherStatus, VoucherVersion,
)
from app.services.accounting import vouchers as engine
from app.services.canonical import content_hash
from app.services.sync import conflicts
from app.core.db import SessionLocal
from app.workers import sync_worker
from app.core.config import settings
from app.models.base import utcnow
from tests.conftest import USER

PAYMENT = {
    "voucher_type": "Payment", "date": "2026-04-10", "narration": "Office tea",
    "entries": [{"ledger": "Office Expenses", "amount": "1200.00"}, {"ledger": "Cash", "amount": "-1200.00"}],
}


def sync():
    return sync_worker.run_once(SessionLocal)


def post(db, company, doc=PAYMENT):
    v = engine.create_voucher(db, company, doc, USER, action="post")
    db.commit()
    return v


def tally_vouchers(tc, vtype="Payment"):
    return [v for v in tc.vouchers.values() if v.findtext("VOUCHERTYPENAME") == vtype]


def test_erp_create_reaches_tally_once_with_stable_identity(db, company, tc):
    v = post(db, company)
    assert v.sync_status == SyncStatus.PENDING
    sync()
    db.refresh(v)
    assert v.sync_status == SyncStatus.SYNCED
    mapping = db.scalar(select(TallyMapping).where(TallyMapping.erp_id == v.id))
    assert mapping.tally_master_id and mapping.tally_guid.startswith(tc.guid)
    created = [x for x in tally_vouchers(tc) if x.findtext("NARRATION") == "Office tea"]
    assert len(created) == 1
    assert created[0].findtext("REMOTEID") == str(v.id)
    # Running sync again (echo of our own write) changes nothing.
    versions = v.version
    sync()
    db.refresh(v)
    assert v.version == versions and v.sync_status == SyncStatus.SYNCED


def test_erp_edit_updates_the_same_tally_voucher(db, company, tc):
    v = post(db, company)
    sync()
    db.refresh(v)
    count = len(tc.vouchers)
    edited = dict(PAYMENT, narration="Office tea and snacks",
                  entries=[{"ledger": "Office Expenses", "amount": "1500"}, {"ledger": "Cash", "amount": "-1500"}])
    engine.update_voucher(db, v, edited, USER, expected_version=v.version)
    db.commit()
    sync()
    db.refresh(v)
    assert len(tc.vouchers) == count, "edit must not create a second Tally voucher"
    mapping = db.scalar(select(TallyMapping).where(TallyMapping.erp_id == v.id))
    tv = tc.vouchers[mapping.tally_master_id]
    assert tv.findtext("NARRATION") == "Office tea and snacks"
    amounts = sorted(e.findtext("AMOUNT") for e in tv.findall("ALLLEDGERENTRIES.LIST"))
    assert amounts == ["-1500.00", "1500.00"]
    assert v.sync_status == SyncStatus.SYNCED


def test_tally_created_voucher_is_imported_with_mapping(db, company, tc, mock):
    sync()   # imports demo voucher
    mid = mock.enter_voucher(tc, """<VOUCHER VCHTYPE="Receipt"><DATE>20260412</DATE>
        <VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME><VOUCHERNUMBER>7</VOUCHERNUMBER>
        <PARTYLEDGERNAME>Acme Traders</PARTYLEDGERNAME><NARRATION>Advance</NARRATION>
        <ALLLEDGERENTRIES.LIST><LEDGERNAME>Acme Traders</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>5000.00</AMOUNT>
          <BILLALLOCATIONS.LIST><NAME>ADV-1</NAME><BILLTYPE>Advance</BILLTYPE><AMOUNT>5000.00</AMOUNT></BILLALLOCATIONS.LIST>
        </ALLLEDGERENTRIES.LIST>
        <ALLLEDGERENTRIES.LIST><LEDGERNAME>HDFC Bank</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>-5000.00</AMOUNT>
          <BANKALLOCATIONS.LIST><TRANSACTIONTYPE>NEFT</TRANSACTIONTYPE><INSTRUMENTNUMBER>UTR123</INSTRUMENTNUMBER><AMOUNT>-5000.00</AMOUNT></BANKALLOCATIONS.LIST>
        </ALLLEDGERENTRIES.LIST></VOUCHER>""")
    sync()
    mapping = db.scalar(select(TallyMapping).where(TallyMapping.tally_master_id == mid, TallyMapping.entity_type == "voucher"))
    v = db.get(Voucher, mapping.erp_id)
    doc = engine.to_doc(v)
    assert v.status == VoucherStatus.POSTED and v.source_system == "tally" and v.sync_status == SyncStatus.SYNCED
    assert doc["voucher_number"] == "7" and doc["party_ledger"] == "Acme Traders"
    acme = next(e for e in doc["entries"] if e["ledger"] == "Acme Traders")
    assert acme["amount"] == "-5000.00" and acme["bills"][0]["name"] == "ADV-1"
    bank = next(e for e in doc["entries"] if e["ledger"] == "HDFC Bank")
    assert bank["bank"][0]["instrument_no"] == "UTR123"
    # Inbound changes never bounce back to Tally.
    assert db.scalar(select(func.count()).select_from(SyncOutbox).where(SyncOutbox.entity_id == v.id)) == 0


def test_tally_edit_updates_erp_and_keeps_history(db, company, tc):
    sync()
    demo_v = db.scalar(select(Voucher).where(Voucher.narration == "April rent"))
    mapping = db.scalar(select(TallyMapping).where(TallyMapping.erp_id == demo_v.id))

    def edit(el):
        el.find("NARRATION").text = "April rent (revised)"
        for e in el.findall("ALLLEDGERENTRIES.LIST"):
            e.find("AMOUNT").text = "-27000.00" if e.findtext("LEDGERNAME") == "Rent" else "27000.00"

    tc.edit_voucher(mapping.tally_master_id, edit)
    sync()
    db.refresh(demo_v)
    assert demo_v.narration == "April rent (revised)"
    assert demo_v.version == 2
    rent = next(e for e in demo_v.entries if e.ledger.name == "Rent")
    assert rent.amount == Decimal("27000.00")
    history = db.scalars(select(VoucherVersion).where(VoucherVersion.voucher_id == demo_v.id).order_by(VoucherVersion.version)).all()
    assert [h.snapshot["narration"] for h in history] == ["April rent", "April rent (revised)"]


def test_cancel_in_erp_cancels_in_tally_and_vice_versa(db, company, tc, mock):
    v = post(db, company)
    sync()
    engine.transition(db, v, "cancel", USER, reason="entered twice")
    db.commit()
    sync()
    mapping = db.scalar(select(TallyMapping).where(TallyMapping.erp_id == v.id))
    assert tc.vouchers[mapping.tally_master_id].findtext("ISCANCELLED") == "Yes"
    db.refresh(v)
    assert v.status == VoucherStatus.CANCELLED and v.sync_status == SyncStatus.SYNCED

    # Tally-side cancellation of the demo voucher
    demo_v = db.scalar(select(Voucher).where(Voucher.narration == "April rent"))
    dm = db.scalar(select(TallyMapping).where(TallyMapping.erp_id == demo_v.id))

    def cancel(el):
        for child in list(el):
            if child.tag.endswith(".LIST"):
                el.remove(child)
        el.find("ISCANCELLED").text = "Yes"

    tc.edit_voucher(dm.tally_master_id, cancel)
    sync()
    db.refresh(demo_v)
    assert demo_v.status == VoucherStatus.CANCELLED


def test_offline_tally_queues_durably_and_recovers(db, company, mock):
    conn = db.get(TallyConnector, company.id)
    real_port = conn.port
    conn.port = 1   # nothing listens here
    db.commit()
    v = post(db, company)
    stats = sync()
    assert stats[str(company.id)]["health"] == "down"
    ev = db.scalar(select(SyncOutbox).where(SyncOutbox.entity_id == v.id))
    assert ev.status == OutboxStatus.PENDING
    conn.port = real_port
    db.commit()
    sync()
    db.refresh(v)
    assert v.sync_status == SyncStatus.SYNCED


def test_lost_response_retry_does_not_duplicate(db, company, tc, mock):
    """Tally applies the write but the response never arrives; the retry must find it, not recreate it."""
    v = post(db, company)
    mock.fail_mode = "drop_after_write"
    sync()
    ev = db.scalar(select(SyncOutbox).where(SyncOutbox.entity_id == v.id))
    db.refresh(ev)
    assert ev.status == OutboxStatus.PENDING and ev.outcome_unknown
    assert len([x for x in tally_vouchers(tc) if x.findtext("REMOTEID") == str(v.id)]) == 1
    mock.fail_mode = None
    sync()
    db.refresh(v)
    assert v.sync_status == SyncStatus.SYNCED
    assert len([x for x in tally_vouchers(tc) if x.findtext("REMOTEID") == str(v.id)]) == 1


def test_duplicate_inbound_delivery_is_idempotent(db, company, tc):
    sync()
    before = db.scalar(select(func.count()).select_from(Voucher))
    conn = db.get(TallyConnector, company.id)
    conn.voucher_watermark = 0          # force a full re-read
    db.commit()
    sync()
    assert db.scalar(select(func.count()).select_from(Voucher)) == before
    assert db.scalar(select(func.count()).select_from(SyncInbox)) == 1


def test_concurrent_edit_raises_conflict_and_resolution_syncs(db, company, tc):
    v = post(db, company)
    sync()
    db.refresh(v)
    mapping = db.scalar(select(TallyMapping).where(TallyMapping.erp_id == v.id))
    # Edit in Tally ...
    tc.edit_voucher(mapping.tally_master_id, lambda el: setattr(el.find("NARRATION"), "text", "Changed in Tally"))
    # ... and in ERP before either side syncs.
    engine.update_voucher(db, v, dict(PAYMENT, narration="Changed in ERP"), USER, expected_version=v.version)
    db.commit()
    sync()
    db.refresh(v)
    conflict = db.scalar(select(SyncConflict).where(SyncConflict.erp_id == v.id))
    assert conflict.status == "open" and v.sync_status == SyncStatus.CONFLICT
    assert v.narration == "Changed in ERP", "no silent overwrite"
    assert tc.vouchers[mapping.tally_master_id].findtext("NARRATION") == "Changed in Tally"
    assert any(d["field"] == "narration" for d in conflict.diff)

    conflicts.resolve(db, conflict, "erp", USER)
    db.commit()
    sync()
    db.refresh(v)
    assert v.sync_status == SyncStatus.SYNCED
    assert tc.vouchers[mapping.tally_master_id].findtext("NARRATION") == "Changed in ERP"
    assert db.scalar(select(SyncConflict.status).where(SyncConflict.id == conflict.id)) == "resolved"


def test_conflict_resolved_with_tally_version(db, company, tc):
    v = post(db, company)
    sync()
    db.refresh(v)
    mapping = db.scalar(select(TallyMapping).where(TallyMapping.erp_id == v.id))
    engine.update_voucher(db, v, dict(PAYMENT, narration="ERP text"), USER, expected_version=v.version)
    db.commit()
    tc.edit_voucher(mapping.tally_master_id, lambda el: setattr(el.find("NARRATION"), "text", "Tally text"))
    # Inbound runs first in the cycle, so this one is detected on the inbound side.
    sync()
    conflict = db.scalar(select(SyncConflict).where(SyncConflict.erp_id == v.id))
    assert conflict.detected_by == "inbound"
    conflicts.resolve(db, conflict, "tally", USER)
    db.commit()
    sync()
    db.refresh(v)
    assert v.narration == "Tally text" and v.sync_status == SyncStatus.SYNCED


def test_missing_master_is_classified_and_operator_queued(db, company, tc):
    # A ledger that exists in ERP only and is not queued for sync (e.g. created before the connector).
    from app.models import Ledger, LedgerGroup
    grp = db.scalar(select(LedgerGroup).where(LedgerGroup.company_id == company.id, LedgerGroup.name == "Indirect Expenses"))
    db.add(Ledger(company_id=company.id, name="Courier", group=grp))
    db.commit()
    v = post(db, company, dict(PAYMENT, entries=[{"ledger": "Courier", "amount": "80"}, {"ledger": "Cash", "amount": "-80"}]))
    sync()
    ev = db.scalar(select(SyncOutbox).where(SyncOutbox.entity_id == v.id))
    assert ev.status == OutboxStatus.FAILED and ev.error_code == "missing_master"
    db.refresh(v)
    assert v.sync_status == SyncStatus.FAILED


def test_new_erp_ledger_syncs_before_voucher_that_uses_it(db, company, tc):
    from app.services.accounting import masters
    masters.create_ledger(db, company, {"name": "Courier", "group": "Indirect Expenses"}, USER)
    v = post(db, company, dict(PAYMENT, entries=[{"ledger": "Courier", "amount": "80"}, {"ledger": "Cash", "amount": "-80"}]))
    sync()
    db.refresh(v)
    assert "Courier" in tc.ledgers
    assert v.sync_status == SyncStatus.SYNCED


def test_freeze_switch_holds_outbound(db, company, tc):
    conn = db.get(TallyConnector, company.id)
    conn.freeze_outbound = True
    db.commit()
    v = post(db, company)
    sync()
    assert db.scalar(select(SyncOutbox.status).where(SyncOutbox.entity_id == v.id)) == OutboxStatus.PENDING
    conn.freeze_outbound = False
    db.commit()
    sync()
    db.refresh(v)
    assert v.sync_status == SyncStatus.SYNCED


def test_invoice_with_inventory_round_trips(db, company, tc, mock):
    tc.add_item("Steel Spool 10in")
    from app.services.sync.initial_import import import_masters
    from app.services.sync import common
    conn = db.get(TallyConnector, company.id)
    client = common.client_for(conn)
    import_masters(db, company, conn, client, USER)
    client.close()
    doc = {
        "voucher_type": "Sales", "date": "2026-04-15", "party_ledger": "Acme Traders", "narration": "Invoice",
        "entries": [{"ledger": "Acme Traders", "amount": "1000",
                     "bills": [{"type": "New Ref", "name": "INV-1", "amount": "1000"}]},
                    {"ledger": "Sales", "amount": "-1000"}],
        "inventory": [{"item": "Steel Spool 10in", "quantity": "-10", "rate": "100", "unit": "Nos", "ledger": "Sales"}],
    }
    v = post(db, company, doc)
    sync()
    db.refresh(v)
    assert v.sync_status == SyncStatus.SYNCED, db.scalar(select(SyncOutbox.last_error).where(SyncOutbox.entity_id == v.id))
    mapping = db.scalar(select(TallyMapping).where(TallyMapping.erp_id == v.id))
    tv = tc.vouchers[mapping.tally_master_id]
    assert tv.find("ALLINVENTORYENTRIES.LIST/ACCOUNTINGALLOCATIONS.LIST/LEDGERNAME").text == "Sales"
    assert [e.findtext("LEDGERNAME") for e in tv.findall("LEDGERENTRIES.LIST")] == ["Acme Traders"]


def test_reconciliation_matches_after_sync(db, company, tc):
    from app.services.sync import reconcile, common
    post(db, company)
    sync()
    conn = db.get(TallyConnector, company.id)
    client = common.client_for(conn)
    run = reconcile.run(db, company, conn, client, date(2026, 4, 1), date(2027, 3, 31), USER)
    client.close()
    assert run.status == "matched", run.result


def test_hash_is_stable_across_round_trip(db, company):
    v = post(db, company)
    assert v.content_hash == content_hash(engine.to_doc(v))


def test_check_tally_matches_company_by_name_and_guid(db, company, mock):
    conn = db.get(TallyConnector, company.id)
    r = sync_worker.check_tally(db, conn)
    assert r["matched"] and r["status"] == "ok" and r["tally_company_name"] == conn.tally_company_name
    assert conn.tally_company_guid == r["guid"]

    real_guid, real_name = conn.tally_company_guid, conn.tally_company_name
    conn.tally_company_guid = "someone-elses-guid"            # same name, different company
    r = sync_worker.check_tally(db, conn)
    assert not r["matched"] and r["status"] == "company_mismatch"
    db.commit()
    v = post(db, company)
    assert sync()[str(company.id)]["health"] == "company_mismatch"   # sync refuses
    db.refresh(v)
    assert v.sync_status != SyncStatus.SYNCED

    conn = db.get(TallyConnector, company.id)
    conn.tally_company_guid, conn.tally_company_name = real_guid, "Old Name"   # renamed in Tally
    assert sync_worker.check_tally(db, conn)["status"] == "company_renamed"
    conn.tally_company_guid = None
    r = sync_worker.check_tally(db, conn)
    assert r["status"] == "company_not_loaded" and r["open_companies"] == [real_name]

    conn.port = 1                                              # nothing listens here
    r = sync_worker.check_tally(db, conn)
    assert r["status"] == "down" and not r["server_running"]


def receipt_xml(day: str, number: str, vtype: str = "Receipt") -> str:
    return f"""<VOUCHER VCHTYPE="{vtype}"><DATE>{day}</DATE><VOUCHERTYPENAME>{vtype}</VOUCHERTYPENAME>
        <VOUCHERNUMBER>{number}</VOUCHERNUMBER>
        <ALLLEDGERENTRIES.LIST><LEDGERNAME>Capital</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>100.00</AMOUNT></ALLLEDGERENTRIES.LIST>
        <ALLLEDGERENTRIES.LIST><LEDGERNAME>HDFC Bank</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>-100.00</AMOUNT></ALLLEDGERENTRIES.LIST>
        </VOUCHER>"""


def voucher_requests(mock, full: bool) -> int:
    return sum(1 for r in mock.requests if "<TYPE>Voucher</TYPE>" in r and ("ALLLEDGERENTRIES" in r) == full)


def test_inbound_asks_tally_for_nothing_when_counters_have_not_moved(db, company, tc, mock):
    sync()
    mock.requests.clear()
    stats = sync()[str(company.id)]["inbound"]
    assert stats["masters"] == "unchanged" and stats["vouchers"] == "unchanged"
    assert voucher_requests(mock, full=False) == 0 and voucher_requests(mock, full=True) == 0


def test_inbound_fetches_only_changed_vouchers_in_batches(db, company, tc, mock, monkeypatch):
    sync()
    monkeypatch.setattr(settings, "sync_voucher_batch_size", 1)
    late = mock.enter_voucher(tc, receipt_xml("20260920", "R-2"))
    early = mock.enter_voucher(tc, receipt_xml("20260408", "R-1"))
    mock.requests.clear()
    stats = sync()[str(company.id)]["inbound"]
    assert stats["received"] == 2 and stats["to_fetch"] == 2
    assert voucher_requests(mock, full=False) == 1     # one header-only change scan
    assert voucher_requests(mock, full=True) == 2      # one MasterID batch per voucher (batch size 1)
    assert all("$MasterID &gt;= " in r for r in mock.requests if "<TYPE>Voucher</TYPE>" in r and "ALLLEDGERENTRIES" in r)
    for mid in (late, early):
        assert db.scalar(select(TallyMapping).where(TallyMapping.tally_master_id == mid,
                                                    TallyMapping.entity_type == "voucher"))


def test_voucher_scans_are_throttled_but_sync_now_forces_one(db, company, tc, mock, monkeypatch):
    sync()
    monkeypatch.setattr(settings, "sync_voucher_scan_seconds", 300)
    mid = mock.enter_voucher(tc, receipt_xml("20260410", "R-9"))
    assert "next scan" in sync()[str(company.id)]["inbound"]["vouchers"]
    find = select(TallyMapping).where(TallyMapping.tally_master_id == mid, TallyMapping.entity_type == "voucher")
    assert db.scalar(find) is None
    sync_worker.run_company(SessionLocal, company.id, force=True)
    assert db.scalar(find) is not None


def test_frozen_voucher_type_is_picked_up_after_unfreezing(db, company, tc, mock):
    sync()
    conn = db.get(TallyConnector, company.id)
    conn.frozen_voucher_types = ["Contra"]
    db.commit()
    contra = mock.enter_voucher(tc, receipt_xml("20260410", "C-1", "Contra"))
    for _ in range(60):   # push later AlterIDs past the overlap window
        mock.enter_voucher(tc, receipt_xml("20260415", f"R-{_}"))
    sync()
    find = select(TallyMapping).where(TallyMapping.tally_master_id == contra, TallyMapping.entity_type == "voucher")
    assert db.scalar(find) is None
    conn = db.get(TallyConnector, company.id)
    conn.frozen_voucher_types = []
    db.commit()
    sync()
    assert db.scalar(find) is not None


SUPPLIER_XML = """
<ADDRESS.LIST TYPE="String"><ADDRESS>Old line</ADDRESS></ADDRESS.LIST>
<LEDMAILINGDETAILS.LIST><APPLICABLEFROM>20230401</APPLICABLEFROM><MAILINGNAME>Anu Engraver</MAILINGNAME>
  <ADDRESS.LIST TYPE="String"><ADDRESS>H. No. 26A</ADDRESS><ADDRESS>Sanjay Colony, Faridabad</ADDRESS></ADDRESS.LIST>
  <PINCODE>121005</PINCODE><STATE>Haryana</STATE><COUNTRY>India</COUNTRY></LEDMAILINGDETAILS.LIST>
<LEDGSTREGDETAILS.LIST><APPLICABLEFROM>20230401</APPLICABLEFROM><GSTREGISTRATIONTYPE>Unregistered</GSTREGISTRATIONTYPE></LEDGSTREGDETAILS.LIST>
<LEDGSTREGDETAILS.LIST><APPLICABLEFROM>20231012</APPLICABLEFROM><GSTREGISTRATIONTYPE>Regular</GSTREGISTRATIONTYPE>
  <GSTIN>06AOZPG4781P1ZW</GSTIN><PLACEOFSUPPLY>Haryana</PLACEOFSUPPLY></LEDGSTREGDETAILS.LIST>
<CONTACTDETAILS.LIST><NAME>Primary Mobile No.</NAME><COUNTRYISDCODE>+91</COUNTRYISDCODE><PHONENUMBER>9205234114</PHONENUMBER>
  <ISDEFAULTWHATSAPPNUM>Yes</ISDEFAULTWHATSAPPNUM></CONTACTDETAILS.LIST>
<CONTACTDETAILS.LIST><NAME>Empty</NAME><COUNTRYISDCODE>+91</COUNTRYISDCODE></CONTACTDETAILS.LIST>
<PAYMENTDETAILS.LIST><TRANSACTIONNAME>Primary</TRANSACTIONNAME><BANKNAME>Indian Bank</BANKNAME>
  <ACCOUNTNUMBER>6336035075</ACCOUNTNUMBER><IFSCODE>IDIB000B137</IFSCODE><PAYMENTFAVOURING>Anu Engraver</PAYMENTFAVOURING>
  <DEFAULTTRANSACTIONTYPE>Inter Bank Transfer</DEFAULTTRANSACTIONTYPE><SETASDEFAULT>Yes</SETASDEFAULT></PAYMENTDETAILS.LIST>
<MSMEREGISTRATIONDETAILS.LIST><FROMDATE>20250401</FROMDATE><ENTERPRISETYPE>Micro</ENTERPRISETYPE>
  <MSMEACTIVITYTYPE>Manufacturing</MSMEACTIVITYTYPE><UDYAMREGNUMBER>UDYAM-HR-03-0023487</UDYAMREGNUMBER></MSMEREGISTRATIONDETAILS.LIST>
"""


def test_ledger_party_details_are_imported(db, company, tc):
    tc.add_ledger("Anu Engraver", "Sundry Creditors", isbillwiseon=True, billcreditperiod="30 Days",
                  incometaxnumber="AOZPG4781P", ledgercontact="Mr Gupta", email="anu@example.com", xml=SUPPLIER_XML)
    sync()
    led = db.scalar(select(Ledger).where(Ledger.company_id == company.id, Ledger.name == "Anu Engraver"))
    assert led.address == "H. No. 26A\nSanjay Colony, Faridabad"   # latest mailing details win over flat lines
    assert (led.pincode, led.state, led.country) == ("121005", "Haryana", "India")
    assert (led.gstin, led.gst_registration_type, led.place_of_supply) == ("06AOZPG4781P1ZW", "Regular", "Haryana")
    assert led.credit_days == 30 and led.pan == "AOZPG4781P"
    assert (led.contact_person, led.email, led.mobile) == ("Mr Gupta", "anu@example.com", "9205234114")
    assert led.contacts == [{"label": "Primary Mobile No.", "isd": "+91", "number": "9205234114", "whatsapp": True}]
    assert led.party_banks[0]["account_no"] == "6336035075" and led.party_banks[0]["ifsc"] == "IDIB000B137"
    assert (led.msme_type, led.msme_udyam_no, led.msme_activity) == ("Micro", "UDYAM-HR-03-0023487", "Manufacturing")
    assert led.msme_from == date(2025, 4, 1)


def test_large_voucher_backlog_is_spread_over_several_passes(db, company, tc, mock, monkeypatch):
    sync()
    monkeypatch.setattr(settings, "sync_voucher_batch_size", 2)
    monkeypatch.setattr(settings, "sync_voucher_max_batches", 1)
    for i in range(5):
        mock.enter_voucher(tc, receipt_xml("20260501", f"B-{i}"))
    passes = []
    for _ in range(4):
        stats = sync()[str(company.id)]["inbound"]
        passes.append(stats["vouchers"] if "vouchers" in stats else (stats["received"], stats["remaining"]))
    assert passes == [(2, 3), (2, 1), (1, 0), "unchanged"]
    assert db.scalar(select(func.count()).select_from(SyncInbox).where(SyncInbox.company_id == company.id,
                                                                       SyncInbox.voucher_number.like("B-%"))) == 5


GROUP_GST_XML = """
<GSTDETAILS.LIST><APPLICABLEFROM>20250401</APPLICABLEFROM><SRCOFGSTDETAILS>Specify Details Here</SRCOFGSTDETAILS>
  <TAXABILITY>Taxable</TAXABILITY><STATEWISEDETAILS.LIST><STATENAME>Any</STATENAME>
  <RATEDETAILS.LIST><GSTRATEDUTYHEAD>CGST</GSTRATEDUTYHEAD><GSTRATE> 9</GSTRATE></RATEDETAILS.LIST>
  <RATEDETAILS.LIST><GSTRATEDUTYHEAD>SGST/UTGST</GSTRATEDUTYHEAD><GSTRATE> 9</GSTRATE></RATEDETAILS.LIST>
  <RATEDETAILS.LIST><GSTRATEDUTYHEAD>IGST</GSTRATEDUTYHEAD><GSTRATE> 18</GSTRATE></RATEDETAILS.LIST>
  </STATEWISEDETAILS.LIST></GSTDETAILS.LIST>
<HSNDETAILS.LIST><APPLICABLEFROM>20250401</APPLICABLEFROM><SRCOFHSNDETAILS>Specify Details Here</SRCOFHSNDETAILS>
  <HSNCODE>83022000</HSNCODE><HSN>Cooler Wheels</HSN></HSNDETAILS.LIST>
"""

ITEM_XML = """
<GSTDETAILS.LIST><APPLICABLEFROM>20250401</APPLICABLEFROM><SRCOFGSTDETAILS>As per Company/Stock Group</SRCOFGSTDETAILS>
  <GSTINELIGIBLEITC>Yes</GSTINELIGIBLEITC></GSTDETAILS.LIST>
<HSNDETAILS.LIST><APPLICABLEFROM>20250401</APPLICABLEFROM><SRCOFHSNDETAILS>As per Company/Stock Group</SRCOFHSNDETAILS></HSNDETAILS.LIST>
<BATCHALLOCATIONS.LIST><GODOWNNAME>VSPL- Patauda</GODOWNNAME><BATCHNAME>Primary Batch</BATCHNAME>
  <OPENINGBALANCE> 1728 PCS =  457.9200 KGS</OPENINGBALANCE><OPENINGRATE>28.00/PCS</OPENINGRATE><OPENINGVALUE>-48384.00</OPENINGVALUE></BATCHALLOCATIONS.LIST>
<STANDARDCOSTLIST.LIST><DATE>20230401</DATE><RATE>25.00/PCS</RATE></STANDARDCOSTLIST.LIST>
<STANDARDCOSTLIST.LIST><DATE>20250401</DATE><RATE>27.50/PCS</RATE></STANDARDCOSTLIST.LIST>
"""


def test_stock_item_details_and_group_gst_are_imported(db, company, tc):
    tc.add_stock_group("Cooler", xml=GROUP_GST_XML)
    tc.add_stock_group("Cooler Wheels", parent="Cooler")
    tc.add_item("3in Wheel", "PCS", parent="Cooler Wheels", additionalunits="KGS", denominator="1", conversion="0.2650",
                category="Not Applicable", costingmethod="Avg. Cost", isbatchwiseon="No", openingbalance=" 1728 PCS",
                openingvalue="-48384.00", closingbalance=" 1700 PCS", closingvalue="-47600.00", xml=ITEM_XML)
    sync()
    wheels = db.scalar(select(StockGroup).where(StockGroup.company_id == company.id, StockGroup.name == "Cooler Wheels"))
    cooler = db.scalar(select(StockGroup).where(StockGroup.company_id == company.id, StockGroup.name == "Cooler"))
    assert wheels.parent_id == cooler.id and cooler.hsn_code == "83022000" and cooler.gst_rate == 18
    item = db.scalar(select(StockItem).where(StockItem.company_id == company.id, StockItem.name == "3in Wheel"))
    assert (item.hsn_code, item.gst_rate, item.gst_source) == ("83022000", 18, "stock group: Cooler")   # inherited two levels up
    assert item.gst_rates == {"CGST": 9.0, "SGST/UTGST": 9.0, "IGST": 18.0} and item.itc_eligible is True
    assert (item.unit, item.alternate_unit, item.denominator, item.conversion) == ("PCS", "KGS", 1, Decimal("0.265"))
    assert item.category is None and item.valuation_method == "Avg. Cost" and item.standard_cost == Decimal("27.5")
    assert (item.opening_qty, item.closing_qty, item.closing_value) == (1728, 1700, Decimal("47600.00"))
    assert item.opening_allocations == [{"godown": "VSPL- Patauda", "batch": "Primary Batch", "qty": "1728",
                                         "rate": "28.00", "value": "48384.00"}]


FULL_LEDGER = {
    "name": "Anu Engraver", "group": "Sundry Creditors", "mailing_name": "Anu Engraver Works",
    "address": "H. No. 26A\nSanjay Colony, Faridabad", "state": "Haryana", "country": "India", "pincode": "121005",
    "is_billwise": True, "credit_days": 30, "opening_balance": Decimal("-1500.00"),
    "pan": "AOZPG4781P", "gst_registration_type": "Regular", "gstin": "06AOZPG4781P1ZW", "place_of_supply": "Haryana",
    "contact_person": "Mr Gupta", "phone": "0129-4000000", "mobile": "9205234114", "email": "anu@example.com",
    "email_cc": "accounts@example.com", "website": "anu.example.com",
    "contacts": [{"label": "Primary Mobile No.", "isd": "+91", "number": "9205234114", "whatsapp": True}],
    "party_banks": [{"label": "Primary", "bank_name": "Indian Bank", "account_no": "6336035075", "ifsc": "IDIB000B137",
                     "favouring": "Anu Engraver", "transfer_type": "Inter Bank Transfer", "default": True}],
    "msme_type": "Micro", "msme_udyam_no": "UDYAM-HR-03-0023487", "msme_activity": "Manufacturing",
    "msme_from": date(2025, 4, 1),
}


def test_ledger_created_in_erp_reaches_tally_with_every_detail(db, company, tc):
    from app.services.accounting import masters
    led = masters.create_ledger(db, company, FULL_LEDGER, USER)
    db.commit()
    sync()   # outbound: Create in Tally
    assert "Anu Engraver" in tc.ledgers and "<UDYAMREGNUMBER>UDYAM-HR-03-0023487" in tc.ledgers["Anu Engraver"]["xml"]
    tc.ledgers["Anu Engraver"]["alter_id"] = tc._next_alter()   # make the next inbound pass re-read it from Tally
    sync()
    db.expire_all()
    led = db.get(Ledger, led.id)
    for field, value in FULL_LEDGER.items():
        if field not in ("name", "group"):
            assert getattr(led, field) == value, field   # what Tally holds is exactly what was entered


def test_sync_reports_progress_step_by_step(db, company, tc, mock, monkeypatch):
    from app.models import SyncProgress
    sync()
    monkeypatch.setattr(settings, "sync_voucher_batch_size", 1)
    for i in range(3):
        mock.enter_voucher(tc, receipt_xml("20260501", f"P-{i}"))
    post(db, company)   # one ERP voucher to send
    seen = []
    from app.services.sync import progress as progress_mod
    real_write = progress_mod.Progress._write

    def spy(self):
        real_write(self)
        seen.append([(s["state"], s["done"], s["total"]) for s in self.data["steps"]])
    monkeypatch.setattr(progress_mod.Progress, "_write", spy)
    sync_worker.run_company(SessionLocal, company.id, force=True, trigger="test")

    row = db.get(SyncProgress, company.id)
    db.refresh(row)
    assert row.data["state"] == "done" and [s["state"] for s in row.data["steps"]] == ["done"] * 4
    assert "3 voucher change(s) received from Tally; 1 sent to Tally" == row.data["summary"]
    vouchers = [snap[2] for snap in seen if snap[2][0] == "running" and snap[2][2] == 3]
    assert [v[1] for v in vouchers] == [0, 1, 2, 3]      # the voucher bar moves batch by batch
    outbound_step = row.data["steps"][3]
    assert (outbound_step["state"], outbound_step["done"], outbound_step["total"]) == ("done", 1, 1)


def test_sync_now_runs_in_background_and_refuses_a_second_run(db, company, mock):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models import SyncProgress, User
    from app.core.security import hash_key
    db.add(User(username="boss", api_key_hash=hash_key("k-boss"), is_superadmin=True))
    db.commit()
    api = TestClient(app)
    r = api.post(f"/api/v1/sync/run?company_id={company.id}", headers={"X-API-Key": "k-boss"})
    assert r.status_code == 202
    p = api.get(f"/api/v1/sync/progress?company_id={company.id}", headers={"X-API-Key": "k-boss"}).json()
    assert p["state"] == "done" and p["trigger"] == "Sync now by boss" and len(p["steps"]) == 4
    row = db.get(SyncProgress, company.id)
    db.refresh(row)
    row.data = {**row.data, "state": "running", "updated_at": utcnow().isoformat()}
    db.commit()
    assert api.post(f"/api/v1/sync/run?company_id={company.id}", headers={"X-API-Key": "k-boss"}).status_code == 409


def test_slow_tally_is_reported_busy_not_down(db, company, mock, monkeypatch):
    import httpx
    from app.connectors.tally import client as client_mod

    def slow(self, *a, **kw):
        raise httpx.ReadTimeout("timed out")
    monkeypatch.setattr(client_mod.httpx.Client, "get", slow)
    r = sync_worker.check_tally(db, db.get(TallyConnector, company.id))
    assert r["status"] == "busy" and r["server_running"] and "answering another request" in r["message"]


def test_check_during_a_sync_does_not_queue_behind_it(db, company, mock):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models import SyncProgress, User
    from app.core.security import hash_key
    db.add(User(username="boss", api_key_hash=hash_key("k-boss"), is_superadmin=True))
    db.add(SyncProgress(company_id=company.id, updated_at=utcnow(), data={
        "state": "running", "updated_at": utcnow().isoformat(),
        "steps": [{"name": "Vouchers from Tally", "state": "running", "detail": "Fetched batch 2 of 5"}]}))
    db.commit()
    mock.requests.clear()
    r = TestClient(app).post(f"/api/v1/sync/connectors/{company.id}/check", headers={"X-API-Key": "k-boss"}).json()
    assert r["status"] == "sync_running" and "Fetched batch 2 of 5" in r["message"] and mock.requests == []


def inv_line(item, qty, amount, inward, unit="Nos", ledger=None, godown="Main Location"):
    signed = -amount if inward else amount
    alloc = (f"<ACCOUNTINGALLOCATIONS.LIST><LEDGERNAME>{ledger}</LEDGERNAME><ISDEEMEDPOSITIVE>{'Yes' if inward else 'No'}"
             f"</ISDEEMEDPOSITIVE><AMOUNT>{signed:.2f}</AMOUNT></ACCOUNTINGALLOCATIONS.LIST>") if ledger else ""
    return (f"<ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>{item}</STOCKITEMNAME><ISDEEMEDPOSITIVE>{'Yes' if inward else 'No'}"
            f"</ISDEEMEDPOSITIVE><AMOUNT>{signed:.2f}</AMOUNT><ACTUALQTY> {qty} {unit}</ACTUALQTY><BILLEDQTY> {qty} {unit}</BILLEDQTY>"
            f"<BATCHALLOCATIONS.LIST><GODOWNNAME>{godown}</GODOWNNAME><BATCHNAME>Primary Batch</BATCHNAME></BATCHALLOCATIONS.LIST>"
            f"{alloc}</ALLINVENTORYENTRIES.LIST>")


def test_stock_only_and_non_accounting_vouchers_follow_tally(db, company, tc, mock):
    from app.reports import ledger_balances, stock_summary
    tc.add_item("Granules", "Kg")
    tc.add_item("Spool", "Nos")
    sync()
    # Manufacturing journal: consumes granules, produces spools, no ledger at all
    mfg = mock.enter_voucher(tc, f"""<VOUCHER VCHTYPE="Stock Journal"><DATE>20260420</DATE>
        <VOUCHERTYPENAME>Stock Journal</VOUCHERTYPENAME><VOUCHERNUMBER>MFG-1</VOUCHERNUMBER>
        {inv_line("Granules", 50, 5000, inward=False, unit="Kg")}{inv_line("Spool", 100, 5000, inward=True)}</VOUCHER>""")
    # Invoice layout as TallyPrime exports it: party in LEDGERENTRIES, sales/purchase ledger on the stock line.
    # Sales order: has ledger lines in Tally's XML, but must not touch the books or the stock
    mock.enter_voucher(tc, f"""<VOUCHER VCHTYPE="Sales Order"><DATE>20260421</DATE>
        <VOUCHERTYPENAME>Sales Order</VOUCHERTYPENAME><VOUCHERNUMBER>SO-1</VOUCHERNUMBER><PARTYLEDGERNAME>Acme Traders</PARTYLEDGERNAME>
        <LEDGERENTRIES.LIST><LEDGERNAME>Acme Traders</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>-90000.00</AMOUNT></LEDGERENTRIES.LIST>
        {inv_line("Spool", 90, 90000, inward=False, ledger="Sales")}</VOUCHER>""")
    # Purchase with a value-only line (rate difference, quantity 0)
    mock.enter_voucher(tc, f"""<VOUCHER VCHTYPE="Purchase"><DATE>20260422</DATE>
        <VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME><VOUCHERNUMBER>P-9</VOUCHERNUMBER><PARTYLEDGERNAME>Bharat Supplies</PARTYLEDGERNAME>
        <LEDGERENTRIES.LIST><LEDGERNAME>Bharat Supplies</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>300.00</AMOUNT></LEDGERENTRIES.LIST>
        {inv_line("Granules", 0, 300, inward=True, unit="Kg", ledger="Purchases")}</VOUCHER>""")
    stats = sync()[str(company.id)]["inbound"]
    assert stats["received"] == 3 and stats["applied"] == 3, [(i.voucher_number, i.error) for i in db.scalars(select(SyncInbox))]

    mapping = db.scalar(select(TallyMapping).where(TallyMapping.tally_master_id == mfg, TallyMapping.entity_type == "voucher"))
    doc = engine.to_doc(db.get(Voucher, mapping.erp_id))
    assert doc["entries"] == [] and len(doc["inventory"]) == 2
    rows = {r["ledger"]: r for r in ledger_balances(db, company.id, date(2027, 3, 31))}
    assert "Sales" not in rows or Decimal(rows["Sales"]["closing"]) == 0           # the order is not a sale
    assert Decimal(rows["Purchases"]["debit"]) == Decimal("300.00")                  # the value-only purchase is
    stock = {r["item"]: r for r in stock_summary(db, company.id, date(2027, 3, 31))["items"]}
    assert (Decimal(stock["Spool"]["inward_qty"]), Decimal(stock["Spool"]["outward_qty"])) == (100, 0)  # order did not move stock
    assert Decimal(stock["Granules"]["outward_qty"]) == 50 and Decimal(stock["Granules"]["inward_value"]) == Decimal("300.00")

    # Re-reading the same vouchers from Tally is recognised as unchanged (hashes agree)
    for v in tc.vouchers.values():
        v.find("ALTERID").text = f" {tc._next_alter()}"
    assert sync()[str(company.id)]["inbound"].get("conflict", 0) == 0


def test_material_out_with_only_the_job_worker_ledger(db, company, tc):
    """As Tally exports a job-work Material Out: one ledger line naming the job worker, stock going out."""
    from app.reports import ledger_balances, stock_summary
    tc.add_item("Spare Part", "Nos")
    sync()
    doc = {"voucher_type": "Material Out", "date": "2026-04-25", "voucher_number": "MO-1", "party_ledger": "Bharat Supplies",
           "entries": [{"ledger": "Bharat Supplies", "amount": "-50000.00"}],
           "inventory": [{"item": "Spare Part", "quantity": "-1", "amount": "50000.00"}]}
    v = engine.create_voucher(db, company, doc, USER, action="post", source="tally")
    db.commit()
    assert v.status == VoucherStatus.POSTED
    bal = {r["ledger"]: r for r in ledger_balances(db, company.id, date(2027, 3, 31))}
    assert Decimal(bal["Bharat Supplies"]["closing"]) == 0                      # non-accounting: party untouched
    stock = {r["item"]: r for r in stock_summary(db, company.id, date(2027, 3, 31))["items"]}
    assert Decimal(stock["Spare Part"]["outward_qty"]) == 1                     # but the stock left

    payment = {"voucher_type": "Payment", "date": "2026-04-25", "entries": [{"ledger": "Cash", "amount": "-10.00"}]}
    with pytest.raises(engine.VoucherError, match="at least two ledger entries"):   # posting types must balance
        engine.create_voucher(db, company, payment, USER, action="post")


def test_stock_item_movement_runs_the_balance(db, company, tc):
    from app.models import StockItem
    from app.reports import stock_item_movement, stock_summary
    tc.add_item("Spool", "Nos", openingbalance=" 10 Nos")
    sync()
    sale = {"voucher_type": "Sales", "date": "2026-04-10", "party_ledger": "Acme Traders",
            "entries": [{"ledger": "Acme Traders", "amount": "400.00"}, {"ledger": "Sales", "amount": "-400.00"}],
            "inventory": [{"item": "Spool", "quantity": "-4", "rate": "100", "amount": "400.00", "ledger": "Sales"}]}
    made = {"voucher_type": "Stock Journal", "date": "2026-04-12", "inventory": [{"item": "Spool", "quantity": "6", "amount": "300.00"}]}
    for doc in (sale, made):
        engine.create_voucher(db, company, doc, USER, action="post")
    db.commit()
    item = db.scalar(select(StockItem).where(StockItem.company_id == company.id, StockItem.name == "Spool"))
    r = stock_item_movement(db, company.id, item.id, date(2026, 4, 1), date(2027, 3, 31))
    assert [(x["outward_qty"], x["inward_qty"], x["balance_qty"]) for x in r["lines"]] == [("4.0000", "", "6.0000"), ("", "6.0000", "12.0000")]
    assert r["opening_qty"] == "10.0000" and r["closing_qty"] == "12.0000"
    row = next(i for i in stock_summary(db, company.id, date(2027, 3, 31))["items"] if i["item"] == "Spool")
    assert row["item_id"] == str(item.id) and row["path"] == []


def boss_client(db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models import User
    from app.core.security import hash_key
    db.add(User(username="boss", api_key_hash=hash_key("k-boss"), is_superadmin=True))
    db.commit()
    c = TestClient(app)
    c.headers["X-API-Key"] = "k-boss"
    return c


def test_every_tally_amendment_is_synced_with_its_history(db, company, tc):
    sync()
    demo_v = db.scalar(select(Voucher).where(Voucher.narration == "April rent"))
    mid = db.scalar(select(TallyMapping).where(TallyMapping.erp_id == demo_v.id)).tally_master_id

    def amount(value):
        def edit(el):
            for e in el.findall("ALLLEDGERENTRIES.LIST"):
                e.find("AMOUNT").text = f"-{value}" if e.findtext("LEDGERNAME") == "Rent" else value
        return edit
    tc.edit_voucher(mid, amount("27000.00"))
    sync()
    tc.edit_voucher(mid, lambda el: setattr(el.find("NARRATION"), "text", "April rent, revised twice"))
    sync()

    api = boss_client(db)
    versions = api.get(f"/api/v1/vouchers/{demo_v.id}/versions").json()
    assert [v["version"] for v in versions] == [1, 2, 3] and {v["source"] for v in versions[1:]} == {"tally"}
    assert {c["field"] for c in versions[1]["changes"]} == {"entries[HDFC Bank]", "entries[Rent]"}
    assert versions[2]["changes"] == [{"field": "narration", "before": "April rent", "after": "April rent, revised twice"}]

    amends = api.get(f"/api/v1/sync/amendments?company_id={company.id}").json()
    assert [(a["version"], a["changes"]) for a in amends] == [(3, ["narration"]), (2, ["entries[HDFC Bank]", "entries[Rent]"])]
    counts = api.get(f"/api/v1/sync/dashboard?company_id={company.id}").json()["vouchers"]
    assert counts == {"in_tally": 1, "synced": 1, "not_stored": 0, "waiting_in_tally": 0, "amended_in_tally": 1}


def test_backlog_scan_shrinks_and_auto_sync_keeps_going(db, company, tc, mock, monkeypatch):
    from app.workers.auto_sync import AutoSync
    sync()
    monkeypatch.setattr(settings, "sync_voucher_batch_size", 2)
    monkeypatch.setattr(settings, "sync_voucher_max_batches", 1)
    monkeypatch.setattr(settings, "sync_voucher_scan_seconds", 300)   # backlog passes must not wait for this
    for i in range(5):
        mock.enter_voucher(tc, receipt_xml("20260501", f"A-{i}"))
    conn = db.get(TallyConnector, company.id)
    conn.voucher_scan_at = None   # the last scan was long enough ago
    db.commit()
    marks, scanned, rounds = [], [], []
    for _ in range(4):
        rounds.append(AutoSync.run_round())
        conn = db.get(TallyConnector, company.id)
        db.refresh(conn)
        marks.append(conn.voucher_watermark)
        row = db.scalar(select(SyncProgress).where(SyncProgress.company_id == company.id))
        db.refresh(row)
        scanned.append(row.data["summary"])
    assert rounds == [True, True, False, False], (marks, scanned)  # next slot straight away while vouchers are waiting
    assert marks[0] < marks[1] < marks[2] == marks[3]     # the watermark moves up after every pass
    assert db.scalar(select(func.count()).select_from(SyncInbox).where(SyncInbox.voucher_number.like("A-%"))) == 5


def test_empty_order_is_accepted_but_an_empty_payment_is_not(db, company):
    order = {"voucher_type": "Purchase Order", "date": "2026-04-17", "party_ledger": "Bharat Supplies", "entries": []}
    assert engine.create_voucher(db, company, order, USER, action="post", source="tally").status == VoucherStatus.POSTED
    # A blank gate pass (Material Out, Tally keeps it) touches neither books nor stock.
    gate_pass = {"voucher_type": "Material Out", "number": "NRGP-8", "date": "2026-04-17",
                 "party_ledger": "Bharat Supplies", "entries": []}
    assert engine.create_voucher(db, company, gate_pass, USER, action="post", source="tally").status == VoucherStatus.POSTED
    with pytest.raises(engine.VoucherError, match="needs ledger entries or stock lines"):
        engine.create_voucher(db, company, {"voucher_type": "Payment", "date": "2026-04-17", "entries": []}, USER, action="post")


def test_auto_sync_switch(db, company):
    from app.workers.auto_sync import auto_sync
    api = boss_client(db)
    r = api.put("/api/v1/sync/auto", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False and auto_sync.paused
    assert api.get(f"/api/v1/sync/dashboard?company_id={company.id}").json()["auto_sync"]["enabled"] is False


def test_reconcile_reads_only_vouchers_changed_in_tally(db, company, tc):
    """Reading every voucher in full made Tally time out and hang; unchanged ones are compared by AlterID."""
    from app.services.sync import reconcile, common
    post(db, company)
    sync()
    mp = db.scalar(select(TallyMapping).where(TallyMapping.company_id == company.id, TallyMapping.entity_type == "voucher"))
    mp.tally_alter_id = int(tc.vouchers[mp.tally_master_id].findtext("ALTERID"))   # as stored after a sync
    db.commit()
    conn = db.get(TallyConnector, company.id)
    client = common.client_for(conn)
    span = (date(2026, 4, 1), date(2027, 3, 31))
    run = reconcile.run(db, company, conn, client, *span, USER)
    assert run.status == "matched", run.result
    read = run.result["read_from_tally"]
    assert read["vouchers_in_range"] >= 1 and read["read_in_full"] == 0 and read["unchanged"] == read["vouchers_in_range"]

    # Somebody alters the voucher in Tally: only that one is read in full, and the difference is found.
    def alter(el):
        n = el.find("NARRATION")
        n.text = "changed in Tally"
    tc.edit_voucher(mp.tally_master_id, alter)
    run = reconcile.run(db, company, conn, client, *span, USER)
    client.close()
    assert run.result["read_from_tally"]["read_in_full"] == 1
    assert len(run.result["content_mismatch"]) == 1, run.result


def test_requests_to_one_tally_take_turns_and_wait_after_a_timeout(monkeypatch):
    import threading
    import time as _time
    import httpx
    from app.connectors.tally import client as tc_mod
    from app.connectors.tally.errors import TallyError
    from app.core.config import settings

    busy = {"now": 0, "most": 0, "posts": 0, "gets": 0, "timeout_once": True, "answer_after_gets": 2}
    lock = threading.Lock()

    def handler(request: httpx.Request):
        if request.method == "GET":
            busy["gets"] += 1
            if busy["gets"] <= busy["answer_after_gets"]:
                raise httpx.ReadTimeout("still working", request=request)
            return httpx.Response(200, text="<RESPONSE>TallyPrime Server is Running</RESPONSE>")
        with lock:
            busy["now"] += 1
            busy["most"] = max(busy["most"], busy["now"])
            busy["posts"] += 1
        try:
            if busy["timeout_once"]:
                busy["timeout_once"] = False
                raise httpx.ReadTimeout("no answer", request=request)
            real_sleep(0.05)
            return httpx.Response(200, text="<ENVELOPE></ENVELOPE>")
        finally:
            with lock:
                busy["now"] -= 1

    monkeypatch.setattr(settings, "tally_recovery_seconds", 30)
    real_sleep = _time.sleep
    monkeypatch.setattr(tc_mod.time, "sleep", lambda s: real_sleep(0.01))
    url_port = 59999
    tc_mod._gates.pop(f"http://gate-test:{url_port}", None)
    make = lambda: tc_mod.TallyClient("gate-test", url_port, "Co", transport=httpx.MockTransport(handler))

    with make() as c:
        with pytest.raises(TallyError, match="timed out"):
            c.post("<ENVELOPE/>")
    assert busy["posts"] == 1
    # The next request first waits until Tally answers again (2 unanswered checks), then goes out.
    with make() as c:
        assert c.post("<ENVELOPE/>")
    assert busy["gets"] == 3 and busy["posts"] == 2

    # Several syncs at once: never more than one request at a time to that Tally.
    def worker():
        with make() as c:
            c.post("<ENVELOPE/>")
    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert busy["most"] == 1 and busy["posts"] == 8


def test_tally_ledger_may_carry_more_than_its_items(db, company, tc):
    """In Tally a purchase or job-work ledger can hold freight or charges besides its items. Such vouchers
    come from Tally already accepted; only vouchers typed here must match their items exactly."""
    from app.services.sync import common
    from app.services.sync.initial_import import import_masters
    tc.add_item("Steel Spool 10in")
    conn = db.get(TallyConnector, company.id)
    client = common.client_for(conn)
    import_masters(db, company, conn, client, USER)
    client.close()
    doc = {"voucher_type": "Sales", "date": "2026-04-15", "party_ledger": "Acme Traders",
           "entries": [{"ledger": "Acme Traders", "amount": "1250"}, {"ledger": "Sales", "amount": "-1250"}],
           "inventory": [{"item": "Steel Spool 10in", "quantity": "-10", "rate": "100", "unit": "Nos", "ledger": "Sales"}]}
    v = engine.create_voucher(db, company, {**doc, "voucher_number": "T-1"}, USER, action="post", source="tally")
    assert v.status == VoucherStatus.POSTED
    with pytest.raises(engine.VoucherError, match="Inventory allocated to 'Sales' is"):
        engine.create_voucher(db, company, {**doc, "voucher_number": "E-1"}, USER, action="post")


def test_long_texts_from_tally_do_not_block_an_item(db, company):
    """A stock group name longer than gst_source once made 7 COOLACE items (and 210 vouchers) fail."""
    from app.models import StockItem
    item = StockItem(company_id=company.id, name="AH 1012-C-F-012-DC-01", unit="Nos",
                     gst_source="stock group: " + "Air Cooled Oil Cooler With Fan Motor 230V AC Single Phase " * 6)
    db.add(item)
    db.commit()
    db.refresh(item)
    assert item.name == "AH 1012-C-F-012-DC-01" and len(item.gst_source) == 250
