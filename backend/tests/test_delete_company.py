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
