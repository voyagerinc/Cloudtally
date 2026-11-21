"""GST reports: GSTR-1 sections and rate lines, GSTR-2, HSN summary, e-invoice and e-way bill status."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import Ledger, StockItem, Voucher
from tests.test_api import admin, books, make_user, voucher  # noqa: F401  (fixtures)

client = TestClient(app)


def _ledger(cid, h, name, group, **extra):
    r = client.post(f"/api/v1/companies/{cid}/masters/ledgers", headers=h, json={"name": name, "group": group, **extra})
    assert r.status_code == 201, r.text


def test_gst_reports(db, books, admin):  # noqa: F811
    h = admin
    _ledger(books, h, "Walk-in Customer", "Sundry Debtors")
    _ledger(books, h, "Big Buyer Unreg", "Sundry Debtors", state="Maharashtra")
    _ledger(books, h, "Steel Supplier", "Sundry Creditors", gstin="27AAACS1234F1Z5")
    _ledger(books, h, "Purchases", "Purchase Accounts")
    _ledger(books, h, "Input IGST", "Duties & Taxes")
    _ledger(books, h, "Advance Received", "Current Liabilities")
    db.query(Ledger).filter_by(company_id=books, name="Acme Traders").one().gstin = "24AAACA1111A1Z1"
    db.commit()
    # B2B sale with IGST (the shared test voucher), plus an advance adjusted on the party's side
    v1 = voucher(books, h).json()
    db.get(Voucher, v1["id"]).statutory = {"irn": "a" * 64, "ack_no": "1124", "ack_date": "2026-04-20",
                                            "eway_bill_no": "331000000001", "eway_bill_date": "2026-04-20"}
    db.commit()
    voucher(books, h, date="2026-04-21", entries=[
        {"ledger": "Acme Traders", "debit": "68000"}, {"ledger": "Advance Received", "debit": "50000"},
        {"ledger": "Sales", "credit": "100000"}, {"ledger": "Output IGST", "credit": "18000"}])
    # unregistered: small and large inter-state
    voucher(books, h, date="2026-04-22", party_ledger="Walk-in Customer", entries=[
        {"ledger": "Walk-in Customer", "debit": "1180"}, {"ledger": "Sales", "credit": "1000"}, {"ledger": "Output IGST", "credit": "180"}])
    voucher(books, h, date="2026-04-23", party_ledger="Big Buyer Unreg", entries=[
        {"ledger": "Big Buyer Unreg", "debit": "236000"}, {"ledger": "Sales", "credit": "200000"}, {"ledger": "Output IGST", "credit": "36000"}])
    # a purchase from a registered supplier
    voucher(books, h, voucher_type="Purchase", date="2026-04-24", party_ledger="Steel Supplier", entries=[
        {"ledger": "Purchases", "debit": "50000"}, {"ledger": "Input IGST", "debit": "9000"}, {"ledger": "Steel Supplier", "credit": "59000"}])
    # a debit note of one rupee to the registered customer: still an e-invoice / e-way bill document
    r = voucher(books, h, voucher_type="Debit Note", date="2026-04-25", entries=[
        {"ledger": "Acme Traders", "debit": "1.18"}, {"ledger": "Sales", "credit": "1"}, {"ledger": "Output IGST", "credit": "0.18"}])
    assert r.status_code == 201, r.text
    q = f"company_id={books}&from_date=2026-04-01&to_date=2026-04-30"

    g1 = client.get(f"/api/v1/reports/gstr1?{q}", headers=h).json()
    sec = {s["section"]: s for s in g1["sections"]}
    assert sec["B2B"]["count"] == 2 and sec["B2B"]["taxable"] == "110000.00" and sec["B2B"]["igst"] == "19800.00"
    assert sec["B2B"]["value"] == "129800.00"                    # 11,800 + 1,18,000: the advance is not a supply
    assert sec["B2CS"]["count"] == 1 and sec["B2CL"]["count"] == 1 and sec["B2CL"]["taxable"] == "200000.00"
    [line] = [r for r in g1["rows"] if r["section"] == "B2CS"]
    assert (line["rate"], line["taxable"], line["igst"]) == ("18.00", "1000.00", "180.00")
    assert g1["total"]["taxable"] == "311000.00"

    g2 = client.get(f"/api/v1/reports/gstr2?{q}", headers=h).json()
    b2b = next(s for s in g2["sections"] if s["section"] == "B2B")       # every Debit Note voucher is also listed here
    assert (b2b["count"], b2b["taxable"], b2b["igst"]) == (1, "50000.00", "9000.00")

    ei = client.get(f"/api/v1/reports/einvoice?{q}", headers=h).json()
    assert ei["counts"] == {"Generated": 1, "Missing": 2, "Not applicable": 2}        # the debit note needs an IRN too
    assert {r["voucher_type"] for r in ei["rows"]} == {"Sales", "Debit Note"}
    gen = next(r for r in ei["rows"] if r["status"] == "Generated")
    assert (gen["ack_no"], gen["ack_date"]) == ("1124", "2026-04-20")

    ew = client.get(f"/api/v1/reports/eway-bill?{q}", headers=h).json()
    assert ew["counts"] == {"Generated": 1, "Not generated": 4}            # every sales invoice and note, any amount
    assert {r["voucher_type"] for r in ew["rows"]} == {"Sales", "Debit Note"}   # no purchases
    assert next(r for r in ew["rows"] if r["voucher_type"] == "Debit Note")["value"] == "1.18"
    assert next(r for r in ew["rows"] if r["status"] == "Generated")["eway_no"] == "331000000001"

    hs = client.get(f"/api/v1/reports/hsn-summary?{q}", headers=h).json()
    assert hs["rows"] == [] and hs["total"]["taxable"] == "0.00"   # no item lines in these invoices

    x = client.get(f"/api/v1/reports/gstr1?{q}&format=xlsx", headers=h)
    assert x.status_code == 200 and x.content[:2] == b"PK"


def test_hsn_summary_and_eway_bill_from_item_lines(db, books, admin):  # noqa: F811
    h = admin
    r = client.post(f"/api/v1/companies/{books}/masters/stock-items", headers=h,
                    json={"name": "Oil Cooler", "unit": "Nos", "hsn_code": "84195090", "gst_rate": 18})
    assert r.status_code == 201, r.text
    db.query(Ledger).filter_by(company_id=books, name="Acme Traders").one().gstin = "24AAACA1111A1Z1"
    db.commit()
    voucher(books, h, date="2026-05-02", entries=[
        {"ledger": "Acme Traders", "debit": "118000"}, {"ledger": "Sales", "credit": "100000"}, {"ledger": "Output IGST", "credit": "18000"}],
        inventory=[{"item": "Oil Cooler", "quantity": "-4", "rate": "25000", "amount": "100000", "unit": "Nos", "ledger": "Sales"}])
    q = f"company_id={books}&from_date=2026-05-01&to_date=2026-05-31"
    [row] = client.get(f"/api/v1/reports/hsn-summary?{q}", headers=h).json()["rows"]
    assert (row["type"], row["hsn"], row["uqc"], row["quantity"], row["rate"]) == ("B2B", "84195090", "NOS", "4", "18.00")
    assert (row["taxable"], row["igst"], row["value"]) == ("100000.00", "18000.00", "118000.00")
    ew = client.get(f"/api/v1/reports/eway-bill?{q}", headers=h).json()
    assert ew["counts"] == {"Not generated": 1} and ew["rows"][0]["value"] == "118000.00"


def test_proforma_invoices_are_not_in_the_gst_reports(db, books, admin):  # noqa: F811
    h = admin
    r = client.post(f"/api/v1/companies/{books}/masters/voucher-types", headers=h,
                    json={"name": "Proforma Invoice", "base_type": "Sales"})
    assert r.status_code == 201, r.text
    db.query(Ledger).filter_by(company_id=books, name="Acme Traders").one().gstin = "24AAACA1111A1Z1"
    db.commit()
    assert voucher(books, h, voucher_type="Proforma Invoice", date="2026-06-02").status_code == 201
    q = f"company_id={books}&from_date=2026-06-01&to_date=2026-06-30"
    assert client.get(f"/api/v1/reports/gstr1?{q}", headers=h).json()["documents"] == []
    assert client.get(f"/api/v1/reports/einvoice?{q}", headers=h).json()["rows"] == []
    assert client.get(f"/api/v1/reports/eway-bill?{q}", headers=h).json()["rows"] == []
    voucher(books, h, date="2026-06-03")                    # a real sale the same month is reported
    assert len(client.get(f"/api/v1/reports/gstr1?{q}", headers=h).json()["documents"]) == 1
