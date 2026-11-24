"""System admin > Backup & restore: the whole database to a .zip and back, to move to another server."""
import io
import json
import time
import zipfile

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import app
from app.models import Company, Ledger, LedgerGroup, User, UserSession
from app.services import backup
from tests.test_api import admin, books, make_user  # noqa: F401  (fixtures)

client = TestClient(app)
API = "/api/v1/admin/backup"


def _wait():
    for _ in range(200):
        if backup.JOB.get("state") != "running":
            return backup.JOB
        time.sleep(0.05)
    raise AssertionError("restore did not finish")


def test_backup_and_restore_round_trip(db, admin, books):
    other = make_user(db, "clerk")
    assert client.get(f"{API}/download", headers=other).status_code == 403

    r = client.get(f"{API}/download", headers=admin)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format"] == "tallycloud-backup" and manifest["tables"]["ledgers"] == db.scalar(
        select(func.count()).select_from(Ledger))
    assert "tables/user_sessions.jsonl" not in zf.namelist()
    before = {"companies": db.scalar(select(func.count()).select_from(Company)),
              "ledgers": sorted(db.scalars(select(Ledger.name))),
              "groups": db.scalar(select(func.count()).select_from(LedgerGroup)),
              "opening": {l.name: l.opening_balance for l in db.scalars(select(Ledger))}}
    assert before["groups"] and any(g.parent_id for g in db.scalars(select(LedgerGroup)))   # groups under groups

    # the new server has other data: all of it is replaced
    assert client.post("/api/v1/companies", json={"name": "Other Books", "books_from": "2026-04-01"},
                       headers=admin).status_code == 201
    make_user(db, "someone-new")

    assert client.post(f"{API}/restore", headers=admin, files={"file": ("b.zip", r.content)},
                       data={"confirm": "no"}).status_code == 422
    bad = client.post(f"{API}/restore", headers=admin, files={"file": ("b.zip", b"not a zip")}, data={"confirm": "RESTORE"})
    assert bad.status_code == 422 and "not a Tally Cloud backup" in bad.json()["detail"]

    ok = client.post(f"{API}/restore", headers=admin, files={"file": ("b.zip", r.content)}, data={"confirm": "RESTORE"})
    assert ok.status_code == 202, ok.text
    job = _wait()
    assert job["state"] == "ok", job

    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Company)) == before["companies"]
    assert db.scalar(select(Company).where(Company.name == "Other Books")) is None
    assert sorted(db.scalars(select(Ledger.name))) == before["ledgers"]
    assert {l.name: l.opening_balance for l in db.scalars(select(Ledger))} == before["opening"]
    assert db.scalar(select(func.count()).select_from(LedgerGroup)) == before["groups"]
    assert db.scalar(select(User).where(User.username == "someone-new")) is None
    assert db.scalar(select(func.count()).select_from(UserSession)) == 0
    # the users came with it: the old server's admin key still works, and the books read as before
    assert client.get(f"{API}/restore", headers=admin).json()["state"] == "ok"
    cid = db.scalar(select(Company.id).where(Company.name == "Spool Works"))
    assert client.get(f"/api/v1/companies/{cid}/masters/ledgers", headers=admin).status_code == 200


def test_backup_from_a_newer_version_is_refused(tmp_path, db, admin):
    path = tmp_path / "b.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": "tallycloud-backup", "format_version": 99}))
    r = client.post(f"{API}/restore", headers=admin, files={"file": ("b.zip", path.read_bytes())},
                    data={"confirm": "RESTORE"})
    assert r.status_code == 422 and "newer version" in r.json()["detail"]


def test_older_schema_is_accepted_and_newer_refused():
    order = backup._revisions()
    assert order[0] < order[-1] and len(order) >= 21

    class Conn:      # stands in for a database at the latest revision
        def __init__(self, rev): self.rev = rev
    import app.services.backup as b
    real = b.schema_revision
    try:
        b.schema_revision = lambda conn: conn.rev
        b.check_compatible({"schema_revision": order[-3]}, Conn(order[-1]))          # older backup: fine
        for newer in (order[-1], "9999"):
            try:
                b.check_compatible({"schema_revision": newer, "app_version": "9.9"}, Conn(order[-3]))
            except backup.BackupError as exc:
                assert "newer version" in str(exc)
            else:
                raise AssertionError("a newer backup was accepted")
    finally:
        b.schema_revision = real


def test_backup_of_chosen_companies_is_imported_next_to_the_others(db, admin, books):
    from tests.test_api import voucher
    from app.models import UserCompanyAccess, Voucher
    assert voucher(books, admin).status_code == 201
    keep = client.post("/api/v1/companies", json={"name": "Keep Me Ltd", "books_from": "2026-04-01"},
                       headers=admin).json()["id"]
    make_user(db, "clerk", company=books, role="accountant")

    r = client.get(f"{API}/download?company={books}", headers=admin)
    assert r.status_code == 200 and "Spool-Works" in r.headers["content-disposition"]
    m = json.loads(zipfile.ZipFile(io.BytesIO(r.content)).read("manifest.json"))
    assert m["scope"] == "companies" and [c["name"] for c in m["companies"]] == ["Spool Works"]
    assert m["tables"]["vouchers"] == 1 and m["tables"]["users"] == 1          # clerk; the admin has no row

    files = {"file": ("b.zip", r.content)}
    info = client.post(f"{API}/inspect", headers=admin, files=files).json()
    assert info["scope"] == "companies" and info["existing"] == ["Spool Works"]
    # the same company is here: only with "replace"
    bad = client.post(f"{API}/restore", headers=admin, files=files, data={"confirm": "IMPORT"})
    assert bad.status_code == 202
    assert _wait()["state"] == "failed" and "Already on this server" in backup.JOB["message"]
    assert client.post(f"{API}/restore", headers=admin, files=files, data={"confirm": "RESTORE"}).status_code == 422

    # another server: without Spool Works and without the clerk
    assert client.delete(f"/api/v1/companies/{books}?confirm=Spool Works", headers=admin).status_code == 200
    db.execute(User.__table__.delete().where(User.username == "clerk"))
    db.commit()
    ok = client.post(f"{API}/restore", headers=admin, files=files, data={"confirm": "IMPORT"})
    assert ok.status_code == 202, ok.text
    job = _wait()
    assert job["state"] == "ok" and job.get("companies"), job

    db.expire_all()
    names = sorted(db.scalars(select(Company.name)))
    assert names == ["Keep Me Ltd", "Spool Works"]
    assert db.scalar(select(func.count()).select_from(Voucher)) == 1
    clerk = db.scalar(select(User).where(User.username == "clerk"))
    assert clerk and not clerk.is_superadmin
    assert db.scalar(select(UserCompanyAccess.role).where(UserCompanyAccess.user_id == clerk.id)) == "accountant"
    assert client.get(f"/api/v1/companies/{keep}", headers=admin).status_code == 200      # untouched

    # import again over it, replacing
    again = client.post(f"{API}/restore", headers=admin, files=files, data={"confirm": "IMPORT", "replace": "true"})
    assert again.status_code == 202 and _wait()["state"] == "ok"
    db.expire_all()
    assert sorted(db.scalars(select(Company.name))) == ["Keep Me Ltd", "Spool Works"]
    assert db.scalar(select(func.count()).select_from(Voucher)) == 1
