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
    from app.licensing import service
    for limit in (service.activation_limit, service.setup_limit):   # per-minute limits: each test starts afresh
        limit.hits.clear()
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
                              ("custom", "custom", ["sync.read", "sync.manage"]),
                              ("puller", "custom", ["sync.read", "sync.pull"])]:
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
    assert api.post(f"{V1}/sync/pull?company_id={cid}", headers=heads["puller"]).status_code != 403
    assert api.post(f"{V1}/sync/push?company_id={cid}", headers=heads["puller"]).status_code == 403
    assert api.post(f"{V1}/sync/pull?company_id={cid}", headers=heads["custom"]).status_code == 403
    assert api.get(f"{V1}/sync/dashboard?company_id={cid}", headers=heads["accadmin"]).status_code == 200
    catalog = api.get(f"{V1}/permissions", headers=heads["manager"]).json()
    keys = {p["key"] for g in catalog["groups"] for p in g["permissions"]}
    assert "sync.manage" not in keys and {"sync.pull", "sync.push"} <= keys


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

    def get_via_portal(url, dest, total=None):
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
    # The page's bars: the download is reported, and the installer's steps are read from its progress file.
    prog = api.get(f"{V1}/licence/update/progress", headers=admin).json()
    assert prog["download"]["state"] == "downloaded" and prog["download"]["version"] == newer and prog["install"] is None
    (tmp_path / "updates" / "progress.json").write_text(
        '{"state": "installing", "percent": 50, "step": "Installing Python packages", "version": "%s"}' % newer, encoding="utf-8-sig")
    inst = api.get(f"{V1}/licence/update/progress", headers=admin).json()["install"]
    assert (inst["state"], inst["percent"], inst["version"]) == ("installing", 50, newer)
    s = api.get(f"{V1}/licence", headers=admin).json()
    assert s["update_available"]["version"] == newer and s["can_install_updates"]
    # Once that version runs, the offer is no longer shown (the update bar goes away by itself).
    st = db.get(LicenseState, 1)
    db.refresh(st)
    offer = dict(st.update_available)
    st.update_available = {**offer, "version": __version__}
    db.commit()
    assert api.get(f"{V1}/licence", headers=admin).json()["update_available"] is None
    st.update_available = offer
    db.commit()

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
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "--allow-empty", "-m", "builder"], check=True)
    run = lambda *a: subprocess.run([sys.executable, str(clone / "deploy" / "make_release.py"), *a, "--out", str(tmp_path / "dist")],
                                    cwd=clone, capture_output=True, text=True)
    before = core.version_tuple((clone / "backend/app/__init__.py").read_text())
    r = run("--bump", "patch")
    assert r.returncode == 0, r.stderr
    version = f"{before[0]}.{before[1]}.{before[2] + 1}"
    with zipfile.ZipFile(tmp_path / "dist" / f"TallyCloud-{version}.zip") as z:
        assert z.read("VERSION").decode().strip() == version
        assert f'__version__ = "{version}"' in z.read("app/backend/app/__init__.py").decode()
        assert "run.bat" in z.namelist() and "app/deploy/windows/run.ps1" in z.namelist() and "app/deploy/windows/install-server.ps1" in z.namelist()
    tags = subprocess.run(["git", "-C", str(clone), "tag", "--list", f"v{version}"], capture_output=True, text=True).stdout
    assert tags.strip() == f"v{version}"
    # Changed code without a new version is refused.
    (clone / "README.md").write_text("changed\n")
    assert run().returncode != 0


def test_github_actions_uploads_with_the_release_token_only(db, admin, portal, monkeypatch, tmp_path):
    import subprocess
    import sys
    env = tmp_path / ".env"
    env.write_text("ERP_DATABASE_URL=x\nERP_RELEASE_TOKEN_HASH=old\n")
    out = subprocess.run([sys.executable, "-m", "app.cli", "release-token", "--env", str(env)],
                         capture_output=True, text=True, check=True).stdout
    token = out.split("Value: ")[1].strip()
    lines = env.read_text().splitlines()
    assert lines[0] == "ERP_DATABASE_URL=x" and len([x for x in lines if x.startswith("ERP_RELEASE_TOKEN_HASH=")]) == 1
    assert token not in env.read_text()                         # only its hash is kept on the server
    monkeypatch.setattr(settings, "release_token_hash", lines[1].split("=", 1)[1])

    def push(version, tok, publish="false"):
        return api.post(f"{V1}/admin/releases/from-github", headers={"X-Release-Token": tok} if tok else {},
                        data={"version": version, "publish": publish, "release_notes": "- Faster sync"},
                        files={"file": (f"TallyCloud-{version}.zip", io.BytesIO(_package(version)), "application/zip")})

    assert push("1.0.0", None).status_code == 401
    assert push("1.0.0", "rel_wrong").status_code == 401
    r = push("1.0.0", token)
    assert r.status_code == 201 and r.json()["status"] == "draft" and r.json()["signature"]
    assert push("1.0.0", token).status_code == 409                   # every update needs a higher version
    r = push("1.0.1", token, publish="true")
    assert r.status_code == 201 and r.json()["status"] == "published"
    rels = api.get(f"{V1}/admin/releases", headers=admin).json()
    assert {(x["version"], x["status"]) for x in rels} == {("1.0.0", "draft"), ("1.0.1", "published")}
    # The token opens nothing else.
    assert api.get(f"{V1}/admin/customers", headers={"X-Release-Token": token}).status_code == 401


def test_portal_publishes_its_own_installer_for_customers(db, admin, portal, tmp_path):
    import zipfile
    from app import __version__
    from app.licensing import package

    # The package never carries secrets or local clutter.
    root = tmp_path / "src"
    for rel, text in {"backend/app/main.py": "app", "backend/.env": "ERP_DATABASE_URL=secret", ".env.local": "x",
                      "backend/app/x.pyc": "b", ".git/config": "g", ".venv/lib.py": "v", "deploy/windows/install-server.ps1": "ps",
                      "keys/license_signing.pem": "KEY", "Spec.docx": "d"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    with zipfile.ZipFile(io.BytesIO(package.build("2.3.4", root, public_key="PUB"))) as z:
        names = set(z.namelist())
        assert z.read("VERSION").decode().strip() == "2.3.4" and z.read("license_public_key.txt").decode().strip() == "PUB"
        bat = z.read("run.bat").decode()       # one file: installs the first time, later starts it
        assert '"%~dp0app\\deploy\\windows\\run.ps1"' in bat and "\r\n" in bat and "\r\r" not in bat
        assert "run.bat" in z.read("README.txt").decode() and "start.bat" in z.read("README.txt").decode()
    assert names == {"VERSION", "run.bat", "README.txt", "license_public_key.txt",
                     "app/backend/app/main.py", "app/deploy/windows/install-server.ps1"}

    # A customer sees "no installer" until the portal publishes its own version; then it is there.
    cid = customer(admin)
    licence(admin, cid)
    h = company_admin(admin, cid, "acme-admin")
    assert api.get(f"{V1}/portal/downloads", headers=h).json() == []
    rel = package.ensure_current_release(db)
    assert rel is not None and rel.status == "published" and rel.signature and rel.version == __version__
    assert package.ensure_current_release(db) is None                      # only once per version
    got = api.get(f"{V1}/portal/downloads", headers=h).json()
    assert [r["version"] for r in got] == [__version__]
    link = api.post(f"{V1}/downloads/{got[0]['id']}/link", headers=h).json()["url"]
    with zipfile.ZipFile(io.BytesIO(api.get(link).content)) as z:
        assert z.read("VERSION").decode().strip() == __version__
        assert "app/backend/requirements.txt" in z.namelist() and not any(n.endswith("/.env") for n in z.namelist())
    r = api.post(f"{V1}/admin/releases/build-current", headers=admin)
    assert r.status_code == 409 and "already released" in r.json()["detail"]


def test_installer_never_packs_business_files(tmp_path):
    from app.licensing.package import package_files
    for name in ("Monthly P&L Oct-26.xlsx", "~$Monthly P&L Oct-26.xlsx", "ledger.csv", "notes.pdf", "run.bat"):
        (tmp_path / name).write_text("x")
    (tmp_path / "localdb").mkdir()
    (tmp_path / "localdb" / "pg.conf").write_text("x")
    assert [rel for _, rel in package_files(tmp_path)] == ["run.bat"]


def test_admins_see_the_key_and_a_new_activation_key_replaces_it(db, admin, portal):
    cid = customer(admin)
    lic = licence(admin, cid)
    key = lic["license_key"]
    assert lic["activation"]["label"] == "Not activated" and lic["key_viewable"]
    # the super admin and the customer's own company admin can show the key again
    assert api.get(f"{V1}/admin/licenses/{lic['id']}/key", headers=admin).json()["license_key"] == key
    boss = company_admin(admin, cid, "acme-boss")
    assert api.get(f"{V1}/portal/licenses/{lic['id']}/key", headers=boss).json()["license_key"] == key
    other = company_admin(admin, customer(admin, "BETA", "Beta Ltd"), "beta-boss")
    assert api.get(f"{V1}/portal/licenses/{lic['id']}/key", headers=other).status_code == 404   # not their licence
    assert api.get(f"{V1}/admin/licenses/{lic['id']}/key", headers=boss).status_code == 403

    assert activate(key).status_code == 200
    out = api.get(f"{V1}/admin/customers/{cid}", headers=admin).json()["license_list"][0]
    assert out["activation"]["activated"] and out["activation"]["label"] == "Activated"
    assert "license_key" not in out                                   # never in the lists, only on Show key

    # reactivation: a new key; the old one stops working and the server must activate again
    r = api.post(f"{V1}/admin/licenses/{lic['id']}/action", headers=admin,
                 json={"action": "new_activation_key", "reason": "server replaced"}).json()
    new = r["license_key"]
    assert new != key and r["released"] == 1 and r["activation"]["label"] == "Not activated"
    assert activate(key, "inst-0000000000000009").status_code != 200
    assert api.get(f"{V1}/portal/licenses/{lic['id']}/key", headers=boss).json()["license_key"] == new
    assert activate(new, "inst-0000000000000009").status_code == 200


def test_the_download_reports_how_far_it_is(tmp_path, monkeypatch):
    import contextlib
    import httpx
    seen = []

    class Resp:
        status_code = 200
        headers = {"content-length": "1000000"}

        def iter_bytes(self, size):
            for _ in range(4):
                seen.append(dict(lic_client.DOWNLOAD))
                yield b"x" * 250000

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: contextlib.nullcontext(Resp()))
    lic_client.DOWNLOAD.clear()
    digest = lic_client._get_file("https://portal/x.zip", tmp_path / "x.part", total=5)
    assert len(digest) == 64 and (tmp_path / "x.part").stat().st_size == 1000000
    assert [s["done"] for s in seen] == [0, 250000, 500000, 750000] and seen[0]["total"] == 1000000   # server's size wins
    assert lic_client.DOWNLOAD["done"] == 1000000


def test_licence_allows_only_the_reports_given_to_the_customer(db, admin, portal):
    """A customer's licence can allow only some reports (e.g. one made for that customer); None means all."""
    cid = customer(admin)
    books = api.post(f"{V1}/companies", headers=admin, json={"name": "Acme Books", "books_from": "2026-04-01"}).json()
    lic = licence(admin, cid)
    assert lic["reports"] is None                                          # default: every report
    assert api.put(f"{V1}/admin/customers/{cid}/companies", headers=admin,
                   json={"company_ids": [books["id"]]}).status_code == 200
    ha = company_admin(admin, cid, "acme-admin")
    q = f"company_id={books['id']}&from_date=2026-04-01&to_date=2026-04-30"
    assert api.get(f"{V1}/reports/trial-balance?{q}", headers=ha).status_code == 200
    r = api.patch(f"{V1}/admin/licenses/{lic['id']}", headers=admin, json={"reports": ["wo-manufacturing", "trial-balance"]})
    assert r.status_code == 200 and [x["code"] for x in r.json()["reports"]] == ["trial-balance", "wo-manufacturing"]
    enforce.forget_cached()
    assert api.get(f"{V1}/reports/trial-balance?{q}", headers=ha).status_code == 200
    r = api.get(f"{V1}/reports/profit-loss?{q}", headers=ha)
    assert r.status_code == 403 and "Profit & Loss" in r.json()["detail"]
    assert api.get(f"{V1}/licence?company_id={books['id']}", headers=ha).json()["reports"] == \
        ["trial-balance", "wo-manufacturing"]
    assert api.patch(f"{V1}/admin/licenses/{lic['id']}", headers=admin, json={"reports": ["nope"]}).status_code == 422
    assert api.patch(f"{V1}/admin/licenses/{lic['id']}", headers=admin, json={"reports": None}).json()["reports"] is None
    assert api.get(f"{V1}/reports/profit-loss?{q}", headers=ha).status_code == 200      # all reports again
    # The installation's signed entitlement carries the list too.
    payload = {"installation_id": "i1", "status": "active", "reports": ["gstr1"]}
    ent = core.entitlement_from_token(payload, "i1", utcnow(), utcnow())
    assert ent.allows_report("gstr1") and not ent.allows_report("gstr2")
    assert core.entitlement_from_token({"installation_id": "i1", "status": "active"}, "i1", utcnow(),
                                       utcnow()).allows_report("gstr2")          # older licence server: all


def test_fixed_assets_is_a_module_of_the_licence(db, admin, portal):
    cid = customer(admin)
    books = api.post(f"{V1}/companies", headers=admin, json={"name": "Acme Books", "books_from": "2026-04-01"}).json()
    lic = licence(admin, cid)
    assert "fixed_assets" in [m["code"] for m in lic["modules"]]          # a new licence has every module
    assert api.put(f"{V1}/admin/customers/{cid}/companies", headers=admin,
                   json={"company_ids": [books["id"]]}).status_code == 200
    ha = company_admin(admin, cid, "acme-admin")
    fa = f"{V1}/fixed-assets"
    assert api.get(f"{fa}/state", headers=ha, params={"company": "Acme Books"}).status_code == 200
    api.patch(f"{V1}/admin/licenses/{lic['id']}", headers=admin, json={"modules": ["accounting", "reports"]})
    enforce.forget_cached()
    r = api.get(f"{fa}/state", headers=ha, params={"company": "Acme Books"})
    assert r.status_code == 403 and "Fixed assets" in r.json()["detail"]
    assert api.get(f"{fa}/status", headers=ha).json()["companies"] == []
    assert "fixed_assets" not in api.get(f"{V1}/licence?company_id={books['id']}", headers=ha).json()["modules"]
    api.patch(f"{V1}/admin/licenses/{lic['id']}", headers=admin, json={"modules": ["accounting", "reports", "fixed_assets"]})
    enforce.forget_cached()
    assert api.get(f"{fa}/state", headers=ha, params={"company": "Acme Books"}).status_code == 200


def test_two_way_sync_only_for_approved_customers(db, admin, portal):
    """Without the two-way module a Tally-connected company works Tally -> software only."""
    import uuid
    from app.models import TallyConnector
    from app.services.sync import outbound
    cid = customer(admin)
    books = api.post(f"{V1}/companies", headers=admin, json={"name": "Acme Books", "books_from": "2026-04-01"}).json()
    lic = licence(admin, cid)
    assert activate(lic["license_key"]).status_code == 200                   # pending until first activated
    enforce.forget_cached()
    mods = [m["code"] for m in lic["modules"]]
    assert "tally_two_way" not in mods and "tally_sync" in mods               # new licence: one way until approved
    assert api.put(f"{V1}/admin/customers/{cid}/companies", headers=admin,
                   json={"company_ids": [books["id"]]}).status_code == 200
    ha = company_admin(admin, cid, "acme-admin")
    new_ledger = lambda name: api.post(f"{V1}/companies/{books['id']}/masters/ledgers", headers=ha,  # noqa: E731
                                       json={"name": name, "group": "Indirect Expenses"})
    r = new_ledger("Rent")
    assert r.status_code == 201, r.text                                       # not connected to Tally: own books
    company_id = uuid.UUID(books["id"])
    db.add(TallyConnector(company_id=company_id, tally_company_name="Acme", enabled=True, outbound_enabled=True))
    db.commit()
    r = new_ledger("Power")
    assert r.status_code == 403 and "Tally to the software only" in r.json()["detail"]
    assert api.get(f"{V1}/licence?company_id={books['id']}", headers=ha).json()["tally_one_way"] is True
    assert api.get(f"{V1}/companies/{books['id']}/vouchers", headers=ha).status_code == 200   # viewing works
    assert outbound.claim(db, 10, company_id) == []                         # nothing is sent to Tally
    conn_url = f"{V1}/sync/connectors/{books['id']}"
    assert api.get(conn_url, headers=ha).json()["two_way_allowed"] is False
    r = api.put(conn_url, headers=ha, json={"tally_company_name": "Acme", "host": "127.0.0.1", "enabled": True,
                                            "sync_mode": "bidirectional", "queued_changes": "send"})
    assert r.status_code == 403 and "one-way sync only" in r.json()["detail"]
    assert api.put(conn_url, headers=ha, json={"tally_company_name": "Acme", "host": "127.0.0.1", "enabled": True,
                                               "sync_mode": "one_way"}).json()["sync_mode"] == "one_way"
    api.patch(f"{V1}/admin/licenses/{lic['id']}", headers=admin, json={"modules": mods + ["tally_two_way"]})
    enforce.forget_cached()
    assert new_ledger("Power").status_code == 201                             # approved: two-way
    assert api.get(conn_url, headers=ha).json()["two_way_allowed"] is True
    r = api.put(conn_url, headers=ha, json={"tally_company_name": "Acme", "host": "127.0.0.1", "enabled": True,
                                            "sync_mode": "bidirectional", "queued_changes": "send"})
    assert r.status_code == 200 and r.json()["sync_mode"] == "bidirectional", r.text
    assert api.get(f"{V1}/licence?company_id={books['id']}", headers=ha).json()["tally_one_way"] is False
    assert len(outbound.claim(db, 10, company_id)) == 1                      # "Power" goes to Tally now
