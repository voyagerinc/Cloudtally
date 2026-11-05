"""Cloud licensing (Cloud Licensing PRD §12 acceptance criteria)."""
import io
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.licensing import client as lic_client
from app.licensing import core, enforce
from app.main import app
from app.models import Company, LicenseState, User
from app.models.base import utcnow
from tests.test_api import admin, make_user  # noqa: F401  (fixture)

api = TestClient(app)
V1 = "/api/v1"


@pytest.fixture()
def portal(tmp_path, monkeypatch):
    """This process is the licence portal, with its own signing key and installer folder."""
    pem, public = core.generate_keypair()
    key = tmp_path / "signing.pem"
    key.write_bytes(pem)
    monkeypatch.setattr(settings, "license_mode", "server")
    monkeypatch.setattr(settings, "license_signing_key_file", str(key))
    monkeypatch.setattr(settings, "license_public_key", public)
    monkeypatch.setattr(settings, "release_dir", str(tmp_path / "releases"))
    core._private_key.cache_clear()
    enforce.forget_cached()
    yield public
    core._private_key.cache_clear()
    enforce.forget_cached()


def customer(admin_headers, code="ACME", name="Acme Industries"):
    r = api.post(f"{V1}/admin/customers", headers=admin_headers,
                 json={"code": code, "legal_name": name, "contact_email": "it@acme.example"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def licence(admin_headers, customer_id, **over):
    body = {"customer_id": customer_id, "plan": "annual", "max_users": 3, "max_installations": 1, **over}
    r = api.post(f"{V1}/admin/licenses", headers=admin_headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def company_admin(admin_headers, customer_id, username):
    """Invited admin who sets their own password with the one-time link, then signs in."""
    r = api.post(f"{V1}/admin/customers/{customer_id}/admins", headers=admin_headers, json={"username": username})
    assert r.status_code == 201, r.text
    token = r.json()["setup_link"].split("#setup=")[1]
    assert api.get(f"{V1}/auth/setup/{token}").json()["username"] == username
    assert api.post(f"{V1}/auth/setup", json={"token": token, "password": f"{username}-Pass9"}).status_code == 200
    assert api.post(f"{V1}/auth/setup", json={"token": token, "password": "Other-pass9"}).status_code == 404  # one use
    r = api.post(f"{V1}/auth/company-admin/login", json={"username": username, "password": f"{username}-Pass9"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def activate(key, installation_id="inst-0000000000000001", version="1.0.0"):
    return api.post(f"{V1}/licenses/activate", json={
        "license_key": key, "installation_id": installation_id, "server_label": "Office server",
        "product_version": version})


def test_create_customer_licence_and_activate(db, admin, portal):
    cid = customer(admin)
    lic = licence(admin, cid, modules=["accounting", "reports"])
    assert lic["license_key"].startswith("TCL-") and lic["status"] == "pending"
    assert "license_key" not in api.get(f"{V1}/admin/customers/{cid}", headers=admin).json()["license_list"][0]

    assert activate("TCL-WRONG-KEY").status_code == 404
    r = activate(lic["license_key"].lower().replace("-", ""))      # typed without dashes, in lower case: fine
    assert r.status_code == 200, r.text
    payload = core.verify(r.json()["token"], portal)
    assert payload["status"] == "active" and payload["modules"] == ["accounting", "reports"]
    assert payload["installation_id"] == "inst-0000000000000001"
    assert core.verify(r.json()["token"][:-4] + "AAAA", portal) is None       # tampered: rejected

    # A second server is over the limit; the same server may activate again.
    r2 = activate(lic["license_key"], "inst-0000000000000002")
    assert r2.status_code == 403 and "maximum" in r2.json()["detail"]
    assert activate(lic["license_key"]).status_code == 200

    # Heartbeat with the installation's own secret; suspension reaches the server on its next check.
    secret = activate(lic["license_key"]).json()["installation_secret"]
    body = {"installation_id": "inst-0000000000000001", "installation_secret": secret}
    assert api.post(f"{V1}/licenses/heartbeat", json=body).status_code == 200
    assert api.post(f"{V1}/licenses/heartbeat", json={**body, "installation_secret": "nope"}).status_code == 401
    api.post(f"{V1}/admin/licenses/{lic['id']}/action", headers=admin, json={"action": "suspend"})
    hb = api.post(f"{V1}/licenses/heartbeat", json=body).json()
    assert core.verify(hb["token"], portal)["status"] == "suspended"
    assert activate(lic["license_key"], "inst-0000000000000003").status_code == 403

    # Release the slot centrally: the old server learns it on its next call.
    inst = api.get(f"{V1}/admin/customers/{cid}", headers=admin).json()["license_list"][0]["installations"][0]
    api.post(f"{V1}/admin/licenses/{lic['id']}/action", headers=admin, json={"action": "resume"})
    api.post(f"{V1}/admin/installations/{inst['id']}/action", headers=admin, json={"action": "release"})
    assert api.post(f"{V1}/licenses/heartbeat", json=body).status_code == 403
    assert activate(lic["license_key"], "inst-0000000000000002").status_code == 200

    events = api.get(f"{V1}/admin/licensing/events?license_id={lic['id']}", headers=admin).json()
    assert {"activate", "heartbeat", "license_suspend", "installation_release"} <= {e["event_type"] for e in events}
    assert any(e["result"] == "rejected" for e in events)


def test_expired_and_version_limits(db, admin, portal):
    cid = customer(admin)
    old = licence(admin, cid, starts_at="2025-01-01", expires_at="2025-12-31")
    r = activate(old["license_key"])
    assert r.status_code == 403 and "expired" in r.json()["detail"]
    capped = licence(admin, cid, max_version="1.4")
    assert activate(capped["license_key"], version="2.0.0").status_code == 403
    assert activate(capped["license_key"], version="1.4.0").status_code == 200
    renewed = api.post(f"{V1}/admin/licenses/{old['id']}/action", headers=admin, json={"action": "renew"}).json()
    assert date.fromisoformat(renewed["expires_at"]) > date.today()


def test_company_admin_sees_only_own_customer(db, admin, portal):
    a, b = customer(admin, "ALPHA", "Alpha Ltd"), customer(admin, "BETA", "Beta Ltd")
    licence(admin, a, max_version="1.9")
    licence(admin, b)
    ha, hb = company_admin(admin, a, "alpha-admin"), company_admin(admin, b, "beta-admin")

    mine = api.get(f"{V1}/portal/license", headers=ha).json()
    assert mine["customer"]["code"] == "ALPHA"
    # Asking for Beta's portal with Beta's id still answers with Alpha's own.
    assert api.get(f"{V1}/portal/license?customer_id={b}", headers=ha).json()["customer"]["code"] == "ALPHA"
    for path in ("/admin/customers", f"/admin/customers/{b}", "/admin/licensing/dashboard", "/admin/releases"):
        assert api.get(V1 + path, headers=ha).status_code == 403, path

    # A release Alpha's licence does not cover: Beta may download it, Alpha may not.
    up = api.post(f"{V1}/admin/releases", headers=admin, data={"version": "2.0.0", "release_notes": "Big update"},
                  files={"file": ("TallyCloud-2.0.0.exe", io.BytesIO(b"MZ installer bytes"), "application/octet-stream")})
    assert up.status_code == 201, up.text
    rel = up.json()
    assert rel["signature"] and len(rel["sha256"]) == 64
    assert api.get(f"{V1}/portal/downloads", headers=hb).json() == []        # still a draft
    api.post(f"{V1}/admin/releases/{rel['id']}/status", headers=admin, json={"status": "published"})
    assert [r["version"] for r in api.get(f"{V1}/portal/downloads", headers=hb).json()] == ["2.0.0"]
    assert api.get(f"{V1}/portal/downloads", headers=ha).json() == []
    assert api.post(f"{V1}/downloads/{rel['id']}/link", headers=ha).status_code == 404

    link = api.post(f"{V1}/downloads/{rel['id']}/link", headers=hb).json()["url"]
    got = api.get(link)
    assert got.status_code == 200 and got.content == b"MZ installer bytes"
    assert api.get(link.replace("?t=", "?t=x")).status_code == 403
    history = api.get(f"{V1}/admin/downloads", headers=admin).json()
    assert {(d["username"], d["result"]) for d in history} == {("alpha-admin", "denied"), ("beta-admin", "ok")}

    # Suspending a customer signs its admins out.
    api.post(f"{V1}/admin/customers/{a}/status", headers=admin, json={"status": "suspended"})
    assert api.get(f"{V1}/portal/license", headers=ha).status_code == 401


def test_hosted_books_follow_the_customer_licence(db, admin, portal):
    cid = customer(admin)
    books = api.post(f"{V1}/companies", headers=admin, json={"name": "Acme Books", "books_from": "2026-04-01"}).json()
    lic = licence(admin, cid, modules=["accounting"], max_users=1)
    assert api.put(f"{V1}/admin/customers/{cid}/companies", headers=admin,
                   json={"company_ids": [books["id"]]}).status_code == 200
    ha = company_admin(admin, cid, "acme-admin")       # gets company admin rights on the hosted books
    q = f"company_id={books['id']}&from_date=2026-04-01&to_date=2026-04-30"
    r = api.get(f"{V1}/reports/trial-balance?{q}", headers=ha)
    assert r.status_code == 403 and "Reports" in r.json()["detail"]          # module not licensed
    assert api.get(f"{V1}/companies/{books['id']}/vouchers", headers=ha).status_code == 200

    # One user at the same time: any number of user IDs, but only one person working at once.
    r = api.post(f"{V1}/companies/{books['id']}/users", headers=ha,
                 json={"username": "clerk", "password": "Clerk-pass9", "role": "accountant"})
    assert r.status_code == 201, r.text
    clerk = r.json()["id"]
    db.get(User, clerk).must_change_password = False
    db.commit()
    r = api.post(f"{V1}/auth/login", json={"username": "clerk", "password": "Clerk-pass9"})
    assert r.status_code == 403 and "All 1 licensed users are working" in r.json()["detail"]
    assert api.get(f"{V1}/licence?company_id={books['id']}", headers=ha).json()["users_used"] == 1
    users = {u["username"]: u for u in api.get(f"{V1}/companies/{books['id']}/users", headers=ha).json()}
    assert users["acme-admin"]["online"] and not users["clerk"]["online"]
    # The admin signing in again in a second browser still holds just one seat.
    assert api.post(f"{V1}/auth/login", json={"username": "acme-admin", "password": "acme-admin-Pass9"}).status_code == 200
    # A browser idle for 15 minutes gives its seat back.
    from app.models import UserSession
    for s in db.query(UserSession).all():
        s.last_seen_at = utcnow() - timedelta(minutes=16)
    db.commit()
    rc = api.post(f"{V1}/auth/login", json={"username": "clerk", "password": "Clerk-pass9"})
    assert rc.status_code == 200
    ch = {"Authorization": f"Bearer {rc.json()['token']}"}
    assert api.post(f"{V1}/auth/ping", headers=ch).status_code == 204
    # Now the clerk holds the seat; the company admin frees it by signing the clerk out.
    r = api.post(f"{V1}/auth/login", json={"username": "acme-admin", "password": "acme-admin-Pass9"})
    assert r.status_code == 403
    for s in db.query(UserSession).all():
        db.refresh(s)
        if str(s.user_id) != clerk:
            s.last_seen_at = utcnow()          # the admin's older browser is still open and active
    db.commit()
    assert api.post(f"{V1}/companies/{books['id']}/users/{clerk}/sign-out", headers=ha).status_code == 200
    assert api.get(f"{V1}/me", headers=ch).status_code == 401

    # Back from lunch: an idle browser must get a seat again, and cannot while someone else holds it.
    for s in db.query(UserSession).all():
        db.refresh(s)
        s.last_seen_at = utcnow() - timedelta(minutes=16)      # the admin went idle: the seat is free
    db.commit()
    r = api.post(f"{V1}/auth/login", json={"username": "clerk", "password": "Clerk-pass9"})
    assert r.status_code == 200
    ch = {"Authorization": f"Bearer {r.json()['token']}"}
    for s in db.query(UserSession).all():
        db.refresh(s)
        s.last_seen_at = utcnow() - timedelta(minutes=20) if str(s.user_id) == clerk else utcnow()
    db.commit()
    r = api.get(f"{V1}/me", headers=ch)
    assert r.status_code == 409 and "seat went to someone else" in r.json()["detail"]

    # Expired: read-only. Viewing still works, changes do not.
    api.patch(f"{V1}/admin/licenses/{lic['id']}", headers=admin,
              json={"starts_at": "2025-01-01", "expires_at": "2025-12-31"})
    assert api.get(f"{V1}/companies/{books['id']}/vouchers", headers=ha).status_code == 200
    r = api.post(f"{V1}/companies/{books['id']}/masters/ledgers", headers=ha,
                 json={"name": "Rent", "group": "Indirect Expenses"})
    assert r.status_code == 403 and "read-only" in r.json()["detail"]
    assert "expired" in api.get(f"{V1}/licence?company_id={books['id']}", headers=ha).json()["reason"]
    # The owner's own books (no customer) are not limited.
    own = api.post(f"{V1}/companies", headers=admin, json={"name": "Voyager Books", "books_from": "2026-04-01"}).json()
    assert api.post(f"{V1}/companies/{own['id']}/masters/ledgers", headers=admin,
                    json={"name": "Rent", "group": "Indirect Expenses"}).status_code == 201


def test_installation_activates_works_offline_then_goes_read_only(db, admin, portal, monkeypatch):
    cid = customer(admin)
    lic = licence(admin, cid, max_users=5, offline_grace_days=7, heartbeat_hours=24)

    def via_portal(base_url, path, body):     # the installation's HTTPS call, answered by this test's portal
        monkeypatch.setattr(settings, "license_mode", "server")
        try:
            r = api.post(V1 + path, json=body)
        finally:
            monkeypatch.setattr(settings, "license_mode", "client")
        if r.status_code >= 400:
            raise lic_client.LicenceError(r.json()["detail"])
        return r.json()

    monkeypatch.setattr(lic_client, "_post", via_portal)
    monkeypatch.setattr(settings, "license_mode", "client")
    enforce.forget_cached()
    books = api.post(f"{V1}/companies", headers=admin, json={"name": "Acme Books", "books_from": "2026-04-01"})
    assert books.status_code == 403 and "not activated" in books.json()["detail"]

    r = api.post(f"{V1}/licence/activate", headers=admin, json={"license_key": lic["license_key"], "server_label": "HO"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "active" and r.json()["activated"]
    books = api.post(f"{V1}/companies", headers=admin, json={"name": "Acme Books", "books_from": "2026-04-01"})
    assert books.status_code == 201
    ledger = {"name": "Rent", "group": "Indirect Expenses"}
    cid_books = books.json()["id"]
    assert api.post(f"{V1}/companies/{cid_books}/masters/ledgers", headers=admin, json=ledger).status_code == 201

    st = db.get(LicenseState, 1)
    db.refresh(st)
    # 3 days without the cloud: still working, with a warning.
    st.last_check_at = utcnow() - timedelta(days=3)
    db.commit()
    enforce.forget_cached()
    s = api.get(f"{V1}/licence", headers=admin).json()
    assert s["state"] == "grace" and s["warnings"]
    # 8 days: read-only, data untouched.
    st.last_check_at = utcnow() - timedelta(days=8)
    db.commit()
    enforce.forget_cached()
    r = api.post(f"{V1}/companies/{cid_books}/masters/ledgers", headers=admin, json={**ledger, "name": "Power"})
    assert r.status_code == 403 and "could not reach" in r.json()["detail"]
    assert api.get(f"{V1}/companies/{cid_books}/masters/ledgers", headers=admin).status_code == 200
    # Setting the clock back does not help.
    st.highest_time_seen = utcnow() + timedelta(days=30)
    st.last_check_at = utcnow() - timedelta(days=1)
    db.commit()
    enforce.forget_cached()
    assert api.get(f"{V1}/licence", headers=admin).json()["read_only"]
    # Connecting again restores everything.
    st.highest_time_seen = None
    db.commit()
    assert api.post(f"{V1}/licence/check", headers=admin).json()["state"] == "active"
    assert api.post(f"{V1}/companies/{cid_books}/masters/ledgers", headers=admin,
                    json={**ledger, "name": "Power"}).status_code == 201

    # Moving to a new server: deactivate gives the slot back.
    assert api.post(f"{V1}/licence/deactivate", headers=admin).json()["state"] == "not_activated"
    monkeypatch.setattr(settings, "license_mode", "server")
    assert activate(lic["license_key"], "inst-new-server-000001").status_code == 200
    assert db.query(Company).count() == 1 and db.query(User).count() >= 1


def test_customer_in_one_step_with_users_and_admin(db, admin, portal):
    body = {"code": "VSPL", "legal_name": "Vikas Spinners Pvt Ltd", "contact_email": "it@vspl.example",
            "license": {"plan": "annual", "max_users": 5, "max_installations": 2},
            "admin": {"username": "vspl-manager", "display_name": "Ramesh"}}
    r = api.post(f"{V1}/admin/customers", headers=admin, json=body)
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["license_key"].startswith("TCL-") and "#setup=" in c["setup_link"] and c["admin_username"] == "vspl-manager"
    detail = api.get(f"{V1}/admin/customers/{c['id']}", headers=admin).json()
    assert detail["license_list"][0]["max_users"] == 5 and detail["license_list"][0]["max_installations"] == 2
    assert detail["admins"][0]["email"] == "it@vspl.example"

    # All or nothing: a taken admin ID creates no customer at all.
    r = api.post(f"{V1}/admin/customers", headers=admin, json={**body, "code": "OTHER", "legal_name": "Other"})
    assert r.status_code == 409
    assert [x["code"] for x in api.get(f"{V1}/admin/customers", headers=admin).json()] == ["VSPL"]

    # The admin chooses their own user ID and password with the link, then signs in to the portal.
    token = c["setup_link"].split("#setup=")[1]
    r = api.post(f"{V1}/auth/setup", json={"token": token, "password": "Ramesh-pass9", "username": "ramesh.vspl"})
    assert r.status_code == 200 and r.json()["username"] == "ramesh.vspl"
    h = api.post(f"{V1}/auth/login", json={"username": "ramesh.vspl", "password": "Ramesh-pass9"})
    assert h.status_code == 200
    p = api.get(f"{V1}/portal/license", headers={"Authorization": f"Bearer {h.json()['token']}"}).json()
    assert p["customer"]["code"] == "VSPL" and p["licenses"][0]["max_users"] == 5


def test_new_installation_first_run_creates_the_manager(db, admin, portal, monkeypatch):
    lic = licence(admin, customer(admin), max_users=5)

    def via_portal(base_url, path, body):
        monkeypatch.setattr(settings, "license_mode", "server")
        try:
            r = api.post(V1 + path, json=body)
        finally:
            monkeypatch.setattr(settings, "license_mode", "client")
        if r.status_code >= 400:
            raise lic_client.LicenceError(r.json()["detail"])
        return r.json()

    monkeypatch.setattr(lic_client, "_post", via_portal)
    monkeypatch.setattr(settings, "license_mode", "client")
    enforce.forget_cached()
    db.query(User).delete()           # a brand-new installation: nobody yet
    db.commit()
    assert api.get(f"{V1}/licence/first-run").json()["needed"]
    bad = api.post(f"{V1}/licence/first-run", json={"license_key": "TCL-NOPE", "username": "manager", "password": "Manager-pass9"})
    assert bad.status_code == 422 and not db.query(User).count()
    r = api.post(f"{V1}/licence/first-run", json={"license_key": lic["license_key"], "server_label": "HO PC",
                                                  "username": "manager", "password": "Manager-pass9"})
    assert r.status_code == 201, r.text
    assert not api.get(f"{V1}/licence/first-run").json()["needed"]
    again = api.post(f"{V1}/licence/first-run", json={"license_key": lic["license_key"], "username": "intruder",
                                                      "password": "Intruder-pass9"})
    assert again.status_code == 409
    t = api.post(f"{V1}/auth/login", json={"username": "manager", "password": "Manager-pass9"}).json()["token"]
    s = api.get(f"{V1}/licence", headers={"Authorization": f"Bearer {t}"}).json()
    assert s["state"] == "active" and s["activated"]


def test_only_the_manager_runs_tally_sync(db, admin):
    from app.core.security import hash_password
    from app.models import UserCompanyAccess
    cid = api.post(f"{V1}/companies", headers=admin, json={"name": "Sync Co", "books_from": "2026-04-01"}).json()["id"]
    heads = {}
    for name, role, perms in [("manager", "owner", None), ("accadmin", "accounts_admin", None),
                              ("custom", "custom", ["sync.read", "sync.manage"])]:
        u = User(username=name, password_hash=hash_password("Some-pass99"))
        db.add(u)
        db.flush()
        db.add(UserCompanyAccess(user_id=u.id, company_id=cid, role=role, permissions=perms))
        db.commit()
        tok = api.post(f"{V1}/auth/login", json={"username": name, "password": "Some-pass99"}).json()["token"]
        heads[name] = {"Authorization": f"Bearer {tok}"}
    assert api.post(f"{V1}/sync/run?company_id={cid}", headers=heads["accadmin"]).status_code == 403
    assert api.post(f"{V1}/sync/run?company_id={cid}", headers=heads["custom"]).status_code == 403
    assert api.post(f"{V1}/sync/run?company_id={cid}", headers=heads["manager"]).status_code != 403
    assert api.get(f"{V1}/sync/dashboard?company_id={cid}", headers=heads["accadmin"]).status_code == 200
    catalog = api.get(f"{V1}/permissions", headers=heads["manager"]).json()
    assert "sync.manage" not in {p["key"] for g in catalog["groups"] for p in g["permissions"]}


def _package(version: str, extra: dict | None = None) -> bytes:
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("VERSION", version + "\n")
        z.writestr("app/backend/app/main.py", "# app\n")
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return buf.getvalue()


def _upload(admin_headers, version, data=None, name=None):
    data = _package(version) if data is None else data
    return api.post(f"{V1}/admin/releases", headers=admin_headers, data={"version": version, "release_notes": f"v{version}"},
                    files={"file": (name or f"TallyCloud-{version}.zip", io.BytesIO(data), "application/zip")})


def test_every_release_needs_a_higher_version(db, admin, portal):
    assert api.get(f"{V1}/admin/releases/next-version", headers=admin).json() == {"latest": None, "next": "1.0.0"}
    assert _upload(admin, "1.0.0").status_code == 201
    r = _upload(admin, "1.0.0")
    assert r.status_code == 409 and "1.0.1 or higher" in r.json()["detail"]
    assert _upload(admin, "0.9.9").status_code == 409
    r = _upload(admin, "1.0.1", data=_package("1.0.5"))          # the package says another version
    assert r.status_code == 422 and "version 1.0.5" in r.json()["detail"]
    assert _upload(admin, "1.1.0").status_code == 201
    assert api.get(f"{V1}/admin/releases/next-version", headers=admin).json() == {"latest": "1.1.0", "next": "1.1.1"}


def test_installation_downloads_checks_and_offers_its_update(db, admin, portal, monkeypatch, tmp_path):
    from app import __version__
    lic = licence(admin, customer(admin), max_version="9")

    def via_portal(base_url, path, body):
        monkeypatch.setattr(settings, "license_mode", "server")
        try:
            r = api.post(V1 + path, json=body)
        finally:
            monkeypatch.setattr(settings, "license_mode", "client")
        if r.status_code >= 400:
            raise lic_client.LicenceError(r.json()["detail"])
        return r.json()

    def get_via_portal(url, dest):
        monkeypatch.setattr(settings, "license_mode", "server")
        try:
            r = api.get(url.split("tallycloud.voyager.co.in", 1)[-1])
        finally:
            monkeypatch.setattr(settings, "license_mode", "client")
        if r.status_code >= 400:
            raise lic_client.LicenceError(r.json()["detail"])
        dest.write_bytes(r.content)
        from hashlib import sha256
        return sha256(r.content).hexdigest()

    monkeypatch.setattr(lic_client, "_post", via_portal)
    monkeypatch.setattr(lic_client, "_get_file", get_via_portal)
    monkeypatch.setattr(settings, "install_dir", str(tmp_path))
    monkeypatch.setattr(settings, "license_mode", "client")
    enforce.forget_cached()
    assert api.post(f"{V1}/licence/activate", headers=admin, json={"license_key": lic["license_key"]}).status_code == 200

    # Nothing newer yet.
    r = api.post(f"{V1}/licence/update/download", headers=admin)
    assert r.status_code == 200 and r.json()["update"] is None

    # A newer published release: downloaded, fingerprint and Voyager's signature checked.
    newer = ".".join(str(int(x) + (1 if i == 0 else 0)) for i, x in enumerate(__version__.split(".")))
    monkeypatch.setattr(settings, "license_mode", "server")
    rel = _upload(admin, newer).json()
    api.post(f"{V1}/admin/releases/{rel['id']}/status", headers=admin, json={"status": "published"})
    monkeypatch.setattr(settings, "license_mode", "client")
    r = api.post(f"{V1}/licence/update/download", headers=admin)
    assert r.status_code == 200, r.text
    upd = r.json()["update"]
    assert upd["version"] == newer and upd["verified"]
    assert (tmp_path / "updates" / f"TallyCloud-{newer}.zip").read_bytes() == _package(newer)
    s = api.get(f"{V1}/licence", headers=admin).json()
    assert s["update_available"]["version"] == newer and s["can_install_updates"]

    # A release whose signature is not Voyager's is never downloaded.
    from app.models import SoftwareRelease
    row = db.get(SoftwareRelease, rel["id"])
    db.refresh(row)
    row.signature = row.signature[::-1]
    db.commit()
    r = api.post(f"{V1}/licence/update/download", headers=admin)
    assert r.status_code == 422 and "not signed by Voyager" in r.json()["detail"]


def test_make_release_bumps_tags_and_stamps_the_version(tmp_path):
    import subprocess
    import sys
    import zipfile
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    clone = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    for k, v in (("user.email", "release@test"), ("user.name", "release test")):
        subprocess.run(["git", "-C", str(clone), "config", k, v], check=True)
    # This working copy's builder and scripts, committed in the clone (they may not be committed here yet).
    import shutil
    for rel in ("deploy/make_release.py", "deploy/windows/install-server.ps1", "deploy/windows/update-server.ps1"):
        shutil.copyfile(repo / rel, clone / rel)
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "-m", "builder"], check=True)
    run = lambda *a: subprocess.run([sys.executable, str(clone / "deploy" / "make_release.py"), *a, "--out", str(tmp_path / "dist")],
                                    cwd=clone, capture_output=True, text=True)
    before = core.version_tuple((clone / "backend/app/__init__.py").read_text())
    r = run("--bump", "patch")
    assert r.returncode == 0, r.stderr
    version = f"{before[0]}.{before[1]}.{before[2] + 1}"
    with zipfile.ZipFile(tmp_path / "dist" / f"TallyCloud-{version}.zip") as z:
        assert z.read("VERSION").decode().strip() == version
        assert f'__version__ = "{version}"' in z.read("app/backend/app/__init__.py").decode()
        assert "install.cmd" in z.namelist() and "app/deploy/windows/install-server.ps1" in z.namelist()
    tags = subprocess.run(["git", "-C", str(clone), "tag", "--list", f"v{version}"], capture_output=True, text=True).stdout
    assert tags.strip() == f"v{version}"
    # Changed code without a new version is refused.
    (clone / "README.md").write_text("changed\n")
    assert run().returncode != 0
