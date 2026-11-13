"""API, security, accounting-engine and report tests (PRD §12)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.db import engine
from app.core.security import hash_key
from app.main import app
from app.models import User, UserCompanyAccess
from tests.conftest import TALLY_COMPANY
from tools.mock_tally import demo

client = TestClient(app)


def make_user(db, name, *, superadmin=False, company=None, role=None) -> dict:
    key = f"key-{name}"
    u = User(username=name, api_key_hash=hash_key(key), is_superadmin=superadmin)
    db.add(u)
    db.flush()
    if company:
        db.add(UserCompanyAccess(user_id=u.id, company_id=company, role=role))
    db.commit()
    return {"X-API-Key": key}


@pytest.fixture()
def admin(db):
    return make_user(db, "admin", superadmin=True)


@pytest.fixture()
def books(admin):
    r = client.post("/api/v1/companies", json={"name": "Spool Works", "books_from": "2026-04-01"}, headers=admin)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    for name, group, ob in [("HDFC Bank", "Bank Accounts", "100000"), ("Capital", "Capital Account", "-100000"),
                            ("Acme Traders", "Sundry Debtors", None), ("Sales", "Sales Accounts", None),
                            ("Rent", "Indirect Expenses", None), ("Output IGST", "Duties & Taxes", None)]:
        r = client.post(f"/api/v1/companies/{cid}/masters/ledgers",
                        json={"name": name, "group": group, "opening_balance": ob, "is_billwise": group == "Sundry Debtors"},
                        headers=admin)
        assert r.status_code == 201, r.text
    return cid


def voucher(cid, headers, **over):
    body = {"voucher_type": "Sales", "date": "2026-04-20", "party_ledger": "Acme Traders", "action": "post",
            "entries": [{"ledger": "Acme Traders", "debit": "11800",
                         "bills": [{"type": "New Ref", "name": "INV-001", "amount": "11800"}]},
                        {"ledger": "Sales", "credit": "10000"}, {"ledger": "Output IGST", "credit": "1800"}]}
    body.update(over)
    return client.post(f"/api/v1/companies/{cid}/vouchers", json=body, headers=headers)


def test_requires_api_key():
    assert client.get("/api/v1/companies").status_code == 401
    assert client.get("/api/v1/companies", headers={"X-API-Key": "nope"}).status_code == 401


def test_cross_company_access_is_blocked(db, books, admin):
    other = client.post("/api/v1/companies", json={"name": "Other Co", "books_from": "2026-04-01"}, headers=admin).json()["id"]
    clerk = make_user(db, "clerk", company=other, role="accountant")
    assert client.get(f"/api/v1/companies/{books}/masters/ledgers", headers=clerk).status_code == 404
    assert voucher(books, clerk).status_code == 404
    assert [c["id"] for c in client.get("/api/v1/companies", headers=clerk).json()] == [other]
    v = voucher(books, admin).json()
    assert client.get(f"/api/v1/vouchers/{v['id']}", headers=clerk).status_code == 404


def test_role_limits_actions(db, books):
    auditor = make_user(db, "aud", company=books, role="auditor")
    assert client.get(f"/api/v1/reports/trial-balance?company_id={books}", headers=auditor).status_code == 200
    assert voucher(books, auditor).status_code == 403
    sales = make_user(db, "sales", company=books, role="sales_purchase")
    assert voucher(books, sales).status_code == 403          # may not post
    assert voucher(books, sales, action="draft").status_code == 201


def test_unbalanced_voucher_rejected(books, admin):
    r = voucher(books, admin, entries=[{"ledger": "Acme Traders", "debit": "100"}, {"ledger": "Sales", "credit": "90"}])
    assert r.status_code == 422 and "differ by 10" in r.json()["detail"]


def test_unknown_ledger_and_bill_mismatch(books, admin):
    r = voucher(books, admin, entries=[{"ledger": "Ghost", "debit": "1"}, {"ledger": "Sales", "credit": "1"}])
    assert r.json()["code"] == "missing_master"
    r = voucher(books, admin, entries=[{"ledger": "Acme Traders", "debit": "100", "bills": [{"name": "X", "amount": "50"}]},
                                       {"ledger": "Sales", "credit": "100"}])
    assert "bill allocations" in r.json()["detail"]


def test_auto_numbering_and_duplicate_check(books, admin):
    a, b = voucher(books, admin).json(), voucher(books, admin).json()
    assert (a["doc"]["voucher_number"], b["doc"]["voucher_number"]) == ("1", "2")
    r = voucher(books, admin, voucher_number="2")
    assert r.status_code == 422 and r.json()["code"] == "duplicate_voucher"


def test_optimistic_concurrency_on_edit(books, admin):
    v = voucher(books, admin).json()
    body = {**v["doc"], "narration": "first edit", "expected_version": v["version"]}
    assert client.patch(f"/api/v1/vouchers/{v['id']}", json=body, headers=admin).status_code == 200
    stale = client.patch(f"/api/v1/vouchers/{v['id']}", json={**body, "narration": "second"}, headers=admin)
    assert stale.status_code == 409
    versions = client.get(f"/api/v1/vouchers/{v['id']}/versions", headers=admin).json()
    assert [x["snapshot"]["narration"] for x in versions] == ["", "first edit"]


def test_maker_checker_approval(db, books, admin):
    vt = next(t for t in client.get(f"/api/v1/companies/{books}/masters/voucher-types", headers=admin).json() if t["name"] == "Payment")
    client.patch(f"/api/v1/companies/{books}/masters/voucher-types/{vt['id']}",
                 json={"requires_approval": True, "approval_threshold": "5000"}, headers=admin)
    maker = make_user(db, "maker", company=books, role="accountant")
    checker = make_user(db, "checker", company=books, role="approver")
    pay = {"voucher_type": "Payment", "date": "2026-04-21", "action": "post",
           "entries": [{"ledger": "Rent", "debit": "20000"}, {"ledger": "HDFC Bank", "credit": "20000"}]}
    v = client.post(f"/api/v1/companies/{books}/vouchers", json=pay, headers=maker).json()
    assert v["status"] == "pending_approval"
    assert client.post(f"/api/v1/vouchers/{v['id']}/approve", headers=maker).status_code == 403
    v = client.post(f"/api/v1/vouchers/{v['id']}/approve", headers=checker).json()
    assert v["status"] == "approved" and v["approved_by"] == "checker"
    assert client.post(f"/api/v1/vouchers/{v['id']}/post", headers=maker).json()["status"] == "posted"
    small = client.post(f"/api/v1/companies/{books}/vouchers", headers=maker, json={
        **pay, "entries": [{"ledger": "Rent", "debit": "100"}, {"ledger": "HDFC Bank", "credit": "100"}]}).json()
    assert small["status"] == "posted"    # under threshold


def test_period_lock(books, admin):
    fy = client.get(f"/api/v1/companies/{books}/financial-years", headers=admin).json()[0]
    client.patch(f"/api/v1/financial-years/{fy['id']}", json={"locked_upto": "2026-04-30"}, headers=admin)
    r = voucher(books, admin)
    assert r.status_code == 422 and r.json()["code"] == "period_locked"
    assert voucher(books, admin, date="2026-05-02").status_code == 201
    assert voucher(books, admin, date="2025-05-02").json()["code"] == "period_locked"


def test_reports_balance(books, admin):
    voucher(books, admin)
    client.post(f"/api/v1/companies/{books}/vouchers", headers=admin, json={
        "voucher_type": "Receipt", "date": "2026-05-05", "action": "post",
        "entries": [{"ledger": "HDFC Bank", "debit": "5000"},
                    {"ledger": "Acme Traders", "credit": "5000", "bills": [{"type": "Agst Ref", "name": "INV-001", "amount": "-5000"}]}]})
    client.post(f"/api/v1/companies/{books}/vouchers", headers=admin, json={
        "voucher_type": "Payment", "date": "2026-05-06", "action": "post",
        "entries": [{"ledger": "Rent", "debit": "2500"}, {"ledger": "HDFC Bank", "credit": "2500"}]})
    voucher(books, admin, action="draft", date="2026-05-07")   # drafts never count
    q = f"company_id={books}&from_date=2026-04-01&to_date=2027-03-31"
    tb = client.get(f"/api/v1/reports/trial-balance?{q}", headers=admin).json()
    # Dr: HDFC 102,500 + Acme 6,800 + Rent 2,500 = Cr: Capital 100,000 + Sales 10,000 + IGST 1,800
    assert tb["difference"] == "0.00" and tb["total_debit"] == "111800.00"
    pl = client.get(f"/api/v1/reports/profit-loss?{q}", headers=admin).json()
    assert pl["net_profit"] == "7500.00"
    bs = client.get(f"/api/v1/reports/balance-sheet?company_id={books}&as_of=2027-03-31", headers=admin).json()
    assert bs["difference"] == "0.00", bs
    os_ = client.get(f"/api/v1/reports/outstanding?company_id={books}&as_of=2026-06-30", headers=admin).json()
    assert os_["total"] == "6800.00" and os_["bills"][0]["bill"] == "INV-001"
    led = next(x for x in tb["lines"] if x["name"] == "HDFC Bank")
    st = client.get(f"/api/v1/reports/ledger?company_id={books}&ledger_id={led['ledger_id']}&from_date=2026-04-01&to_date=2027-03-31",
                    headers=admin).json()
    assert st["opening"] == "100000.00" and st["closing"] == "102500.00" and len(st["lines"]) == 2


def test_cancelled_voucher_leaves_reports(books, admin):
    v = voucher(books, admin).json()
    client.post(f"/api/v1/vouchers/{v['id']}/cancel", json={"reason": "wrong party"}, headers=admin)
    tb = client.get(f"/api/v1/reports/trial-balance?company_id={books}&from_date=2026-04-01&to_date=2027-03-31", headers=admin).json()
    assert tb["total_debit"] == "100000.00"


def test_csv_export_is_audited(books, admin):
    voucher(books, admin)
    r = client.get(f"/api/v1/reports/day-book?company_id={books}&from_date=2026-04-01&to_date=2026-04-30&format=csv", headers=admin)
    assert r.status_code == 200 and r.text.startswith("id,date,voucher_type")
    audit = client.get(f"/api/v1/audit?company_id={books}&action=report.export", headers=admin).json()
    assert len(audit) == 1 and audit[0]["actor"] == "admin"


def test_sensitive_fields_masked(books, admin):
    client.post(f"/api/v1/companies/{books}/masters/ledgers", headers=admin,
                json={"name": "ICICI", "group": "Bank Accounts", "bank_account_no": "001234567890", "pan": "ABCDE1234F"})
    led = next(x for x in client.get(f"/api/v1/companies/{books}/masters/ledgers?q=ICICI", headers=admin).json())
    assert led["bank_account_no"].endswith("7890") and "1234567" not in led["bank_account_no"]


def test_audit_log_is_append_only(books):
    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE audit_events SET actor = 'mallory'"))
    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM audit_events"))


def test_connector_rejects_public_tally_host(books, admin):
    r = client.put(f"/api/v1/sync/connectors/{books}", json={"host": "8.8.8.8", "tally_company_name": "X"}, headers=admin)
    assert r.status_code == 422
    r = client.put(f"/api/v1/sync/connectors/{books}", json={"host": "192.168.1.20", "tally_company_name": "X"}, headers=admin)
    assert r.status_code == 200 and r.json()["outbound_enabled"] is False


def test_correlation_id_round_trips(books, admin):
    r = client.get(f"/api/v1/companies/{books}", headers={**admin, "X-Correlation-ID": "abc123"})
    assert r.headers["X-Correlation-ID"] == "abc123"


def test_ui_is_served():
    r = client.get("/")
    assert r.status_code == 200 and "Tally Cloud" in r.text


def test_open_multiple_tally_companies_with_separate_books(admin, mock):
    demo(mock, "Second Co")
    target = {"host": "127.0.0.1", "port": mock.port}
    found = client.post("/api/v1/import/tally/discover", json=target, headers=admin).json()["companies"]
    assert {c["name"]: c["erp_company_id"] for c in found} == {TALLY_COMPANY: None, "Second Co": None}

    ids = {}
    for name in (TALLY_COMPANY, "Second Co"):
        r = client.post("/api/v1/import/tally/link", json={**target, "tally_company_name": name}, headers=admin)
        assert r.status_code == 201, r.text
        ids[name] = r.json()["company_id"]
    assert ids[TALLY_COMPANY] != ids["Second Co"]

    again = client.post("/api/v1/import/tally/link", json={**target, "tally_company_name": "Second Co",
                                                            "erp_company_name": "Copy"}, headers=admin)
    assert again.status_code == 409
    found = client.post("/api/v1/import/tally/discover", json=target, headers=admin).json()["companies"]
    assert {c["name"]: c["erp_company_id"] for c in found} == ids

    # Each linked company holds only its own Tally company's masters
    for name, cid in ids.items():
        conn = client.get(f"/api/v1/sync/connectors/{cid}", headers=admin).json()
        assert conn["tally_company_name"] == name and conn["outbound_enabled"] is False
        assert client.get(f"/api/v1/companies/{cid}/masters/ledgers", headers=admin).json()


def test_party_bank_accounts_are_masked_unless_revealed(db, books, admin):
    body = {"name": "Anu Engraver", "group": "Sundry Creditors", "pan": "AOZPG4781P", "address": "H. No. 26A\nFaridabad",
            "mobile": "9205234114", "msme_type": "Micro", "msme_udyam_no": "UDYAM-HR-03-0023487",
            "party_banks": [{"bank_name": "Indian Bank", "account_no": "6336035075", "ifsc": "IDIB000B137"}]}
    assert client.post(f"/api/v1/companies/{books}/masters/ledgers", json=body, headers=admin).status_code == 201
    url = f"/api/v1/companies/{books}/masters/ledgers?q=Anu"
    auditor = make_user(db, "aud", company=books, role="auditor")
    masked = client.get(url, headers=auditor).json()[0]
    assert masked["party_banks"][0]["account_no"] == "••••••5075" and masked["pan"] == "••••••781P"
    assert masked["address"] == "H. No. 26A\nFaridabad" and masked["msme_udyam_no"] == "UDYAM-HR-03-0023487"
    assert client.get(url + "&reveal=true", headers=auditor).status_code == 403
    assert client.get(url + "&reveal=true", headers=admin).json()[0]["party_banks"][0]["account_no"] == "6336035075"


def test_ledger_form_checks_formats_and_edits(db, books, admin):
    url = f"/api/v1/companies/{books}/masters/ledgers"
    bad = client.post(url, json={"name": "X", "group": "Sundry Creditors", "pan": "ABC", "gstin": "06AOZ"}, headers=admin)
    assert bad.status_code == 422 and "PAN must be" in bad.text and "GSTIN must be" in bad.text
    bad = client.post(url, json={"name": "X", "group": "Sundry Creditors",
                                 "party_banks": [{"bank_name": "HDFC", "account_no": "123456", "ifsc": "HDFC123"}]}, headers=admin)
    assert bad.status_code == 422 and "IFSC must be" in bad.text
    mismatch = client.post(url, json={"name": "X", "group": "Sundry Creditors", "pan": "AOZPG4781P",
                                      "gstin": "06ABCDE1234F1Z5"}, headers=admin)
    assert mismatch.status_code == 422 and "does not contain the PAN" in mismatch.text

    led = client.post(url, json={"name": "Anu Engraver", "group": "Sundry Creditors", "pan": " aozpg4781p ",
                                 "email": "anu@example.com", "is_billwise": True, "credit_days": 15}, headers=admin).json()
    assert led["pan"] == "AOZPG4781P"
    other = client.post(url, json={"name": "Other Party", "group": "Sundry Creditors"}, headers=admin).json()
    renamed = client.patch(f"{url}/{other['id']}", json={"name": "anu engraver"}, headers=admin)
    assert renamed.status_code == 422 and "already exists" in renamed.text
    edited = client.patch(f"{url}/{led['id']}", json={"email": None, "mobile": "9205234114", "expected_version": led["version"]},
                          headers=admin)
    assert edited.status_code == 200 and edited.json()["email"] is None and edited.json()["mobile"] == "9205234114"
    stale = client.patch(f"{url}/{led['id']}", json={"mobile": "1", "expected_version": led["version"]}, headers=admin)
    assert stale.status_code == 409


def test_old_tally_data_does_not_block_unrelated_edits(db, books, admin):
    from app.models import Ledger
    url = f"/api/v1/companies/{books}/masters/ledgers"
    led = client.post(url, json={"name": "Legacy Party", "group": "Sundry Debtors"}, headers=admin).json()
    row = db.get(Ledger, led["id"])
    row.pan, row.gstin = "AGOPS1077K1", "06ABCDE1234F1Z5"   # as imported from Tally: bad PAN, mismatching GSTIN
    db.commit()
    assert client.patch(f"{url}/{led['id']}", json={"mobile": "9999999999"}, headers=admin).status_code == 200


def test_next_financial_year_opens_by_itself(db, books, admin):
    """Like Tally: books from 1 Apr 2026, a voucher in May 2027 opens 2027-28; a typo in 2040 does not."""
    from app.models import FinancialYear
    r = voucher(books, admin, date="2027-05-10", number="NY-1")
    assert r.status_code == 201, r.text
    years = sorted((fy.start_date.isoformat(), fy.end_date.isoformat())
                   for fy in db.query(FinancialYear).filter(FinancialYear.company_id == books))
    assert years == [("2026-04-01", "2027-03-31"), ("2027-04-01", "2028-03-31")]
    r = voucher(books, admin, date="2040-01-01", number="NY-2")
    assert r.status_code == 422 and r.json()["code"] == "period_locked" and "5 years" in r.json()["detail"]
    assert voucher(books, admin, date="2026-03-31", number="NY-3").json()["code"] == "period_locked"  # before books


def test_long_hsn_descriptions_from_tally_are_kept(db, books):
    """Tally allows long HSN/SAC descriptions; they once broke the stock item import (varchar 300)."""
    from app.models import StockItem
    text_ = "Ethernet patch panel, 24 port, rack mount, " * 20      # about 900 characters
    db.add(StockItem(company_id=books, name="Patch Panel 24", unit="Nos", hsn_description=text_))
    db.commit()
    assert len(db.query(StockItem).filter_by(company_id=books, name="Patch Panel 24").one().hsn_description) == len(text_)


def test_sales_register_matches_tally(db, books, admin):
    """Invoice value = the party's amount; tax = GST ledgers only (Tally marks other ledgers "Others");
    proforma invoices are memos and stay out, as in Tally's Sales Register."""
    from app.models import Ledger, VoucherType
    db.query(Ledger).filter(Ledger.company_id == books).update({"tax_type": "Others"})
    db.query(Ledger).filter_by(company_id=books, name="Output IGST").update({"tax_type": "GST", "gst_duty_head": "IGST"})
    db.add(VoucherType(company_id=books, name="Proforma Invoice", base_type="Sales"))
    db.commit()
    assert voucher(books, admin, number="INV-1").status_code == 201
    assert voucher(books, admin, number="PF-1", voucher_type="Proforma Invoice",
                   entries=[{"ledger": "Acme Traders", "debit": "5900"}, {"ledger": "Sales", "credit": "5000"},
                            {"ledger": "Output IGST", "credit": "900"}]).status_code == 201
    r = client.get(f"/api/v1/reports/register?company_id={books}&base_type=Sales&from_date=2026-04-01&to_date=2026-04-30",
                   headers=admin).json()
    assert [(v["amount"], v["tax"]) for v in r["vouchers"]] == [("11800.00", "1800.00")]
    assert r["total"] == "11800.00" and r["not_included"] == [{"voucher_type": "Proforma Invoice", "vouchers": 1}]
