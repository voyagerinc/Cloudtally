"""Fixed assets & depreciation inside Tally Cloud: the page, its data from the books, saved registers, journal."""
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import hash_key
from app.main import app
from app.models import LedgerGroup, User, Voucher
from app.services.accounting import masters
from app.services.accounting import vouchers as engine
from tests.conftest import USER

api = TestClient(app)
FA = "/api/v1/fixed-assets"


def _books(db, company):
    if not db.scalar(select(LedgerGroup).where(LedgerGroup.company_id == company.id, LedgerGroup.name == "Fixed Assets")):
        masters.create_group(db, company, {"name": "Fixed Assets", "nature": "asset"}, USER)
    masters.create_group(db, company, {"name": "Computers & IT", "parent": "Fixed Assets"}, USER)
    masters.create_ledger(db, company, {"name": "Laptops", "group": "Computers & IT", "opening_balance": "100000"}, USER)
    masters.create_ledger(db, company, {"name": "Depreciation", "group": "Indirect Expenses"}, USER)
    db.commit()
    engine.create_voucher(db, company, {"voucher_type": "Journal", "date": "2026-06-01", "narration": "new laptop",
                                        "entries": [{"ledger": "Laptops", "amount": "50000"},
                                                    {"ledger": "Cash", "amount": "-50000"}]}, USER, action="post")
    db.add(User(username="fa-boss", api_key_hash=hash_key("k-fa"), is_superadmin=True))
    db.commit()
    return {"X-API-Key": "k-fa"}


def test_fixed_assets_page_reads_the_books_saves_and_posts_the_journal(db, company):
    h = _books(db, company)
    page = api.get("/fixed-assets")
    assert page.status_code == 200 and "/api/v1/fixed-assets" in page.text and "erp_token" in page.text

    assert api.get(f"{FA}/auth", headers=h).json()["user"] == "fa-boss"
    names = [c["name"] for c in api.get(f"{FA}/status", headers=h).json()["companies"]]
    assert company.name in names

    data = api.post(f"{FA}/fetch", headers=h, json={"company": company.name, "from": "2026-04-01", "to": "2027-03-31"}).json()
    [laptops] = [a for a in data["assets"] if a["name"] == "Laptops"]
    assert laptops["group"] == "Computers & IT" and laptops["openingTally"] == 100000
    [v] = laptops["vouchers"]
    assert (v["date"], v["amount"], v["others"]) == ("2026-06-01", -50000, ["Cash"])     # Tally sign: debit negative
    dep = next(x for x in data["ledgers"] if x["name"] == "Depreciation")
    assert dep["isExpense"] and not dep["isFixedAsset"] and "Fixed Assets" in data["faGroups"]

    state = {"company": company.name, "from": "2026-04-01", "to": "2027-03-31", "assets": [{"name": "Laptops"}]}
    assert api.post(f"{FA}/state", headers=h, json={"company": company.name, "state": state}).json()["ok"]
    [saved] = api.get(f"{FA}/saved", headers=h).json()["saved"]
    assert saved["company"] == company.name and saved["assetCount"] == 1 and saved["savedBy"] == "fa-boss"
    assert api.get(f"{FA}/state", headers=h, params={"company": company.name}).json()["state"]["assets"] == [{"name": "Laptops"}]

    r = api.post(f"{FA}/post-journal", headers=h, json={
        "company": company.name, "date": "2027-03-31", "narration": "Depreciation FY 2026-27",
        "lines": [{"ledger": "Depreciation", "dr": 45000, "cr": 0}, {"ledger": "Laptops", "dr": 0, "cr": 45000}]}).json()
    assert r["CREATED"] == "1" and r["ERRORS"] == "0", r
    v = db.scalar(select(Voucher).where(Voucher.company_id == company.id, Voucher.narration == "Depreciation FY 2026-27"))
    assert v.status == "posted" and sorted(Decimal(e.amount) for e in v.entries) == [Decimal("-45000"), Decimal("45000")]
    bad = api.post(f"{FA}/post-journal", headers=h, json={"company": company.name, "date": "2027-03-31",
                                                          "lines": [{"ledger": "No Such Ledger", "dr": 1, "cr": 0},
                                                                    {"ledger": "Laptops", "dr": 0, "cr": 1}]}).json()
    assert bad["CREATED"] == "0" and bad["lineErrors"]


def test_fixed_assets_need_a_sign_in_and_the_company(db, company):
    assert api.get(f"{FA}/status").status_code == 401
    db.add(User(username="fa-nobody", api_key_hash=hash_key("k-nobody")))
    db.commit()
    h = {"X-API-Key": "k-nobody"}
    assert api.get(f"{FA}/status", headers=h).json()["companies"] == []
    assert api.get(f"{FA}/state", headers=h, params={"company": company.name}).status_code == 404
