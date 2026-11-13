"""Sales and purchase analysis, FIFO costing, and raw-material cost from production vouchers."""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import StockItem, User
from app.reports import analysis
from app.services.accounting import vouchers as engine
from app.services.sync.initial_import import import_masters
from app.services.sync import common
from tests.conftest import USER

D = Decimal
SPAN = (date(2026, 4, 1), date(2027, 3, 31))


@pytest.fixture()
def books(db, company, tc):
    """Buy a component at 100 then 120, make 2 finished coolers from 4 components, sell one at 500."""
    for name in ("Component A", "Finished Cooler"):
        tc.add_item(name)
    from app.models import TallyConnector
    conn = db.get(TallyConnector, company.id)
    client = common.client_for(conn)
    import_masters(db, company, conn, client, USER)
    client.close()
    db.query(StockItem).filter_by(company_id=company.id, name="Finished Cooler").one().category = "FINISH GOODS"
    db.commit()

    def purchase(no, d, qty, rate):
        amt = str(qty * rate)
        engine.create_voucher(db, company, {
            "voucher_type": "Purchase", "voucher_number": no, "date": d, "party_ledger": "Bharat Supplies",
            "entries": [{"ledger": "Bharat Supplies", "amount": amt}, {"ledger": "Purchases", "amount": f"-{amt}"}],
            "inventory": [{"item": "Component A", "quantity": str(qty), "rate": str(rate), "unit": "Nos",
                           "ledger": "Purchases"}]}, USER, action="post", source="tally")

    purchase("P-1", "2026-04-05", 10, 100)
    purchase("P-2", "2026-05-05", 10, 120)
    engine.create_voucher(db, company, {                     # production: 4 components (FIFO 100 each) -> 2 coolers
        "voucher_type": "Stock Journal", "voucher_number": "MJ-1", "date": "2026-06-01", "entries": [],
        "inventory": [{"item": "Component A", "quantity": "-4", "rate": "100", "amount": "400", "unit": "Nos"},
                      {"item": "Finished Cooler", "quantity": "2", "rate": "200", "amount": "400", "unit": "Nos"}]},
        USER, action="post", source="tally")
    engine.create_voucher(db, company, {
        "voucher_type": "Sales", "voucher_number": "S-1", "date": "2026-06-10", "party_ledger": "Acme Traders",
        "entries": [{"ledger": "Acme Traders", "amount": "500"}, {"ledger": "Sales", "amount": "-500"}],
        "inventory": [{"item": "Finished Cooler", "quantity": "-1", "rate": "500", "unit": "Nos", "ledger": "Sales"}]},
        USER, action="post", source="tally")
    return company


def test_sales_and_purchase_analysis_by_party_month_item_and_category(db, books):
    s = analysis.trade_analysis(db, books.id, "sales", *SPAN, by="party")
    assert [(r["name"], r["value"]) for r in s["rows"]] == [("Acme Traders", "500.00")]
    assert analysis.trade_analysis(db, books.id, "sales", *SPAN, by="month")["rows"][0]["name"] == "Jun 2026"
    cat = analysis.trade_analysis(db, books.id, "sales", *SPAN, by="category")["rows"]
    assert [(r["name"], r["quantity"]) for r in cat] == [("FINISH GOODS", "1")]
    p = analysis.trade_analysis(db, books.id, "purchase", *SPAN, by="item")
    assert [(r["name"], r["quantity"], r["value"]) for r in p["rows"]] == [("Component A", "20", "2200.00")]
    detail = analysis.trade_analysis(db, books.id, "purchase", *SPAN, by="voucher")
    assert [line["voucher_number"] for line in detail["lines"]] == ["P-1", "P-2"]


def test_raw_material_cost_comes_from_the_production_voucher(db, books):
    r = analysis.trade_analysis(db, books.id, "sales", *SPAN, by="item", raw_material_cost=True)
    row = r["rows"][0]
    assert (row["name"], row["value"], row["rm_cost"], row["margin"], row["margin_pct"]) == \
        ("Finished Cooler", "500.00", "200.00", "300.00", "60.00")      # 400 of components / 2 made = 200 each


def test_fifo_costs_issues_at_the_oldest_price(db, books):
    rows = {r["item"]: r for r in analysis.fifo_costing(db, books.id, *SPAN)["items"]}
    a = rows["Component A"]
    assert (a["in_qty"], a["in_value"], a["out_qty"], a["out_cost"]) == ("20", "2200.00", "4", "400.00")
    assert (a["closing_qty"], a["closing_value"]) == ("16", "1800.00")   # 6 left at 100 and 10 at 120
    f = rows["Finished Cooler"]
    assert (f["sold_value"], f["sold_cost"], f["gross_profit"], f["closing_value"]) == ("500.00", "200.00", "300.00", "200.00")


def test_report_permissions_and_api(db, books):
    from app.core.security import hash_key
    db.add(User(username="boss", api_key_hash=hash_key("k-boss"), is_superadmin=True))
    db.commit()
    api = TestClient(app)
    h = {"X-API-Key": "k-boss"}
    q = f"company_id={books.id}&from_date=2026-04-01&to_date=2027-03-31"
    assert api.get(f"/api/v1/reports/sales-analysis?{q}&by=month", headers=h).json()["total"]["value"] == "500.00"
    assert api.get(f"/api/v1/reports/sales-rm-cost?{q}", headers=h).json()["total"]["margin"] == "300.00"
    assert api.get(f"/api/v1/reports/fifo-costing?{q}", headers=h).status_code == 200
    csv = api.get(f"/api/v1/reports/purchase-analysis?{q}&by=voucher&format=csv", headers=h)
    assert csv.status_code == 200 and "P-2" in csv.text
    f = api.get(f"/api/v1/reports/filters?company_id={books.id}", headers=h).json()
    assert "FINISH GOODS" in f["categories"] and any(p["name"] == "Acme Traders" for p in f["parties"])
