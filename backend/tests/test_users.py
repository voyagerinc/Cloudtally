"""Sign-in with user ID and password, and users managed by their company admin."""
from fastapi.testclient import TestClient

from app.core.security import hash_password
from app.main import app
from app.models import User, UserCompanyAccess
from tests.test_api import admin, make_user  # noqa: F401  (fixture)

client = TestClient(app)
API = "/api/v1"


def login(username, password):
    return client.post(f"{API}/auth/login", json={"username": username, "password": password})


def bearer(r) -> dict:
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def company(admin_headers, name="Spool Works"):
    r = client.post(f"{API}/companies", json={"name": name, "books_from": "2026-04-01"}, headers=admin_headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def company_admin(db, cid, name="meera", password="Meera#2026"):
    """A company admin who signs in with a password."""
    u = User(username=name, password_hash=hash_password(password))
    db.add(u)
    db.flush()
    db.add(UserCompanyAccess(user_id=u.id, company_id=cid, role="owner"))
    db.commit()
    return bearer(login(name, password))


def test_password_sign_in_lockout_and_sign_out(db, admin):
    cid = company(admin)
    h = company_admin(db, cid)
    assert login("MEERA", "Meera#2026").status_code == 200           # user ID is not case-sensitive
    me = client.get(f"{API}/me", headers=h).json()
    assert me["username"] == "meera" and me["companies"][0]["role_label"] == "Company admin"

    for _ in range(5):
        assert login("meera", "wrong-one").status_code == 401
    r = login("meera", "Meera#2026")                                   # locked now, even with the right password
    assert r.status_code == 401 and "Too many" in r.json()["detail"]
    assert login("nobody", "whatever1").json()["detail"] == "Wrong user ID or password"

    assert client.post(f"{API}/auth/logout", headers=h).status_code == 204
    assert client.get(f"{API}/me", headers=h).status_code == 401


def test_company_admin_creates_user_who_must_change_password(db, admin):
    cid = company(admin)
    h = company_admin(db, cid)
    r = client.post(f"{API}/companies/{cid}/users", headers=h,
                    json={"username": "ravi", "display_name": "Ravi", "password": "First#pass1", "role": "accountant"})
    assert r.status_code == 201, r.text
    assert r.json()["must_change_password"] and r.json()["role_label"] == "Accountant"
    assert client.post(f"{API}/companies/{cid}/users", headers=h,
                       json={"username": "Ravi", "password": "Other#pass1"}).status_code == 409

    rh = bearer(login("ravi", "First#pass1"))
    assert client.get(f"{API}/me", headers=rh).status_code == 200
    r = client.get(f"{API}/companies", headers=rh)
    assert r.status_code == 403 and "Change your password" in r.json()["detail"]
    assert client.post(f"{API}/auth/password", headers=rh,
                       json={"current_password": "First#pass1", "new_password": "short"}).status_code == 422
    assert client.post(f"{API}/auth/password", headers=rh,
                       json={"current_password": "First#pass1", "new_password": "Ravi-own-pw9"}).status_code == 200
    assert client.get(f"{API}/companies", headers=rh).status_code == 200
    assert login("ravi", "First#pass1").status_code == 401
    assert login("ravi", "Ravi-own-pw9").status_code == 200


def test_custom_permissions_limit_reports(db, admin):
    cid = company(admin)
    h = company_admin(db, cid)
    r = client.post(f"{API}/companies/{cid}/users", headers=h, json={
        "username": "viewer", "password": "View#pass1", "role": "custom",
        "permissions": ["report.trial-balance", "report.sales-register"]})
    assert r.status_code == 201, r.text
    assert r.json()["custom"] and r.json()["role_label"] == "Custom"
    db.get(User, r.json()["id"]).must_change_password = False
    db.commit()
    vh = bearer(login("viewer", "View#pass1"))

    q = f"company_id={cid}&from_date=2026-04-01&to_date=2026-04-30"
    assert client.get(f"{API}/reports/trial-balance?{q}", headers=vh).status_code == 200
    assert client.get(f"{API}/reports/register?{q}&base_type=Sales", headers=vh).status_code == 200
    assert client.get(f"{API}/reports/register?{q}&base_type=Purchase", headers=vh).status_code == 403
    assert client.get(f"{API}/reports/balance-sheet?company_id={cid}", headers=vh).status_code == 403
    assert client.get(f"{API}/reports/trial-balance?{q}&format=csv", headers=vh).status_code == 403   # no export
    assert client.get(f"{API}/companies/{cid}/vouchers", headers=vh).status_code == 403
    assert client.get(f"{API}/companies/{cid}/users", headers=vh).status_code == 403

    # A ready-made role with exactly the role's ticks is stored as that role, not as custom.
    r = client.patch(f"{API}/companies/{cid}/users/{r.json()['id']}", headers=h,
                     json={"role": "reports_viewer", "permissions": None})
    assert r.status_code == 200 and not r.json()["custom"] and r.json()["role_label"] == "Reports only"
    assert client.get(f"{API}/reports/balance-sheet?company_id={cid}", headers=vh).status_code == 200


def test_company_admin_cannot_exceed_own_rights_or_touch_other_companies(db, admin):
    a = company(admin, "Alpha Ltd")
    b = company(admin, "Beta Ltd")
    ha = company_admin(db, a, "alpha-admin", "Alpha#2026")
    hb = company_admin(db, b, "beta-admin", "Beta#20261")

    # Alpha's admin sees and manages Alpha only.
    assert client.get(f"{API}/companies/{b}/users", headers=ha).status_code == 404
    shared = client.post(f"{API}/companies/{b}/users", headers=hb,
                         json={"username": "sunil", "password": "Sunil#pass1", "role": "accountant"}).json()
    # Adding Beta's user to Alpha needs both companies to be Alpha admin's: refused.
    r = client.post(f"{API}/companies/{a}/users", headers=ha,
                    json={"username": "sunil", "role": "accountant", "add_existing": True})
    assert r.status_code == 403
    # The super admin may; afterwards Alpha's admin still cannot reset Sunil's password.
    assert client.post(f"{API}/companies/{a}/users", headers=admin,
                       json={"username": "sunil", "role": "accountant", "add_existing": True}).status_code == 201
    r = client.patch(f"{API}/companies/{a}/users/{shared['id']}", headers=ha, json={"password": "Taken#over1"})
    assert r.status_code == 403 and "company you do not manage" in r.json()["detail"]
    # ... but may change what Sunil can do in Alpha.
    assert client.patch(f"{API}/companies/{a}/users/{shared['id']}", headers=ha,
                        json={"role": "auditor"}).status_code == 200

    # An accounts admin (no user management of their own) gets user management: refused.
    acc = client.post(f"{API}/companies/{a}/users", headers=ha,
                      json={"username": "kiran", "password": "Kiran#pass1", "role": "accounts_admin"}).json()
    u = db.get(User, acc["id"])
    u.must_change_password = False
    db.commit()
    db.get(UserCompanyAccess, (u.id, a)).permissions = sorted(
        {"users.manage", "voucher.read", "masters.read", "reports.read"})
    db.commit()
    kh = bearer(login("kiran", "Kiran#pass1"))
    r = client.post(f"{API}/companies/{a}/users", headers=kh, json={
        "username": "hari", "password": "Hari#pass12", "role": "custom", "permissions": ["voucher.read", "voucher.post"]})
    assert r.status_code == 403 and "voucher.post" in r.json()["detail"]

    # Nobody changes their own permissions or switches themselves off.
    me = client.get(f"{API}/me", headers=ha).json()
    assert client.patch(f"{API}/companies/{a}/users/{me['id']}", headers=ha, json={"role": "accountant"}).status_code == 403
    assert client.patch(f"{API}/companies/{a}/users/{me['id']}", headers=ha, json={"is_active": False}).status_code == 403


def test_switch_off_and_remove_sign_the_user_out(db, admin):
    cid = company(admin)
    h = company_admin(db, cid)
    new = client.post(f"{API}/companies/{cid}/users", headers=h,
                      json={"username": "temp", "password": "Temp#pass12", "role": "accountant"}).json()
    th = bearer(login("temp", "Temp#pass12"))
    assert client.patch(f"{API}/companies/{cid}/users/{new['id']}", headers=h, json={"is_active": False}).status_code == 200
    assert client.get(f"{API}/me", headers=th).status_code == 401
    assert login("temp", "Temp#pass12").status_code == 401
    assert client.patch(f"{API}/companies/{cid}/users/{new['id']}", headers=h, json={"is_active": True}).status_code == 200
    assert client.delete(f"{API}/companies/{cid}/users/{new['id']}", headers=h).status_code == 204
    assert not db.get(User, new["id"]).is_active                      # no company left: switched off
    assert client.get(f"{API}/companies/{cid}/users", headers=h).json()[0]["username"] == "meera"
