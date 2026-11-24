"""An admin deletes a company: everything of it goes, other companies and the audit trail stay."""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.db import Base
from app.models import AuditEvent, Company, Voucher
from app.services.accounting.delete_company import _filters
from tests.test_api import admin, books, make_user, voucher  # noqa: F401  (fixtures)

client = TestClient(app := __import__("app.main", fromlist=["app"]).app)


def _rows_of(db, cid) -> dict:
    out = {}
    for t in Base.metadata.sorted_tables:
        f = _filters(uuid.UUID(cid)).get(t.name)
        if f is not None:
            n = db.scalar(select(func.count()).select_from(t).where(f))
            if n:
                out[t.name] = n
    return out


def test_admin_deletes_a_company_and_only_that_one(db, admin, books):
    assert voucher(books, admin).status_code == 201
    other = client.post("/api/v1/companies", json={"name": "Keep Me Ltd", "books_from": "2026-04-01"},
                        headers=admin).json()["id"]
    kept_before = _rows_of(db, other)
    assert _rows_of(db, books)["vouchers"] == 1 and _rows_of(db, books)["voucher_entries"] == 3

    clerk = make_user(db, "clerk", company=books, role="accountant")
    assert client.delete(f"/api/v1/companies/{books}?confirm=Spool Works", headers=clerk).status_code == 403
    owner = make_user(db, "boss", company=books, role="owner")
    r = client.delete(f"/api/v1/companies/{books}?confirm=wrong", headers=owner)
    assert r.status_code == 422 and "Spool Works" in r.json()["detail"]

    r = client.delete(f"/api/v1/companies/{books}?confirm=spool works", headers=owner)
    assert r.status_code == 200, r.text
    assert r.json()["rows"]["vouchers"] == 1

    db.expire_all()
    assert db.get(Company, uuid.UUID(books)) is None
    assert _rows_of(db, books) == {}
    assert _rows_of(db, other) == kept_before                      # the other company is untouched
    assert db.scalar(select(func.count()).select_from(Voucher)) == 0
    ev = db.scalar(select(AuditEvent).where(AuditEvent.action == "company.delete"))
    assert ev and ev.after["rows_deleted"]["vouchers"] == 1
    assert client.get(f"/api/v1/companies/{books}", headers=admin).status_code == 404
    assert [c["id"] for c in client.get("/api/v1/companies", headers=admin).json()] == [other]


def test_linked_tally_company_can_be_deleted(db, admin, company):
    """A company read from Tally: connector, mappings and sync history go with it."""
    cid, name = str(company.id), company.name
    rows = _rows_of(db, cid)
    assert rows.get("tally_connectors") == 1 and rows.get("tally_mappings")
    r = client.delete(f"/api/v1/companies/{cid}?confirm={name}", headers=admin)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert _rows_of(db, cid) == {}


def test_company_admin_clears_data_and_it_is_read_again_from_tally(db, company, tc, mock):
    """Delete data and read again from Tally: the company, its users and its connector stay; masters and
    vouchers come back from Tally."""
    from app.models import Ledger, TallyConnector, UserCompanyAccess
    from tests.test_sync import receipt_xml, sync
    cid, name = str(company.id), company.name
    mock.enter_voucher(tc, receipt_xml("20260501", "RS-1"))
    sync()
    owner = make_user(db, "boss", company=company.id, role="owner")
    clerk = make_user(db, "clerk", company=company.id, role="accountant")
    before = _rows_of(db, cid)
    assert before["vouchers"] >= 1 and before["ledgers"]

    assert client.post(f"/api/v1/companies/{cid}/reset?confirm={name}", headers=clerk).status_code == 403
    assert client.post(f"/api/v1/companies/{cid}/reset?confirm=nope", headers=owner).status_code == 422
    r = client.post(f"/api/v1/companies/{cid}/reset?confirm={name}", headers=owner)
    assert r.status_code == 202, r.text
    assert r.json()["rows_deleted"]["vouchers"] == before["vouchers"]

    db.expire_all()        # the background read has run: everything is back from Tally
    after = _rows_of(db, cid)
    assert after["vouchers"] == before["vouchers"] and after["ledgers"] == before["ledgers"]
    assert db.get(TallyConnector, company.id).enabled
    assert db.scalar(select(func.count()).select_from(UserCompanyAccess).where(UserCompanyAccess.company_id == company.id)) == 2
    assert db.scalar(select(Ledger).where(Ledger.company_id == company.id, Ledger.name == "HDFC Bank"))


def test_company_admin_restores_the_company_from_its_backup(db, admin, books):
    import time
    from app.services import backup
    owner = make_user(db, "boss", company=books, role="owner")
    assert voucher(books, owner).status_code == 201
    r = client.get(f"/api/v1/companies/{books}/backup", headers=owner)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    files = {"file": ("b.zip", r.content)}

    assert voucher(books, owner, date="2026-04-21").status_code == 201        # made after the backup
    assert db.scalar(select(func.count()).select_from(Voucher).where(Voucher.company_id == uuid.UUID(books))) == 2

    other = client.post("/api/v1/companies", json={"name": "Not This One", "books_from": "2026-04-01"}, headers=admin).json()["id"]
    wrong = client.get(f"/api/v1/companies/{other}/backup", headers=admin).content
    bad = client.post(f"/api/v1/companies/{books}/restore", headers=owner, files={"file": ("w.zip", wrong)},
                      data={"confirm": "Spool Works"})
    assert bad.status_code == 422 and "not of Spool Works" in bad.json()["detail"]

    ok = client.post(f"/api/v1/companies/{books}/restore", headers=owner, files=files, data={"confirm": "Spool Works"})
    assert ok.status_code == 202, ok.text
    for _ in range(200):
        if backup.JOB.get("state") != "running":
            break
        time.sleep(0.05)
    job = client.get(f"/api/v1/companies/{books}/restore", headers=owner).json()
    assert job["state"] == "ok", job
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Voucher).where(Voucher.company_id == uuid.UUID(books))) == 1
    assert client.get(f"/api/v1/companies/{books}", headers=owner).status_code == 200      # still the admin of it
    assert client.get(f"/api/v1/companies/{other}", headers=admin).status_code == 200      # other company untouched
