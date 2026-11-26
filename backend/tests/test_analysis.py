"""Sales and purchase analysis, FIFO costing, and raw-material cost from production vouchers."""
import uuid
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
    r = analysis.trade_analysis(db, books.id, "sales", *SPAN, by="item", cost="rm")
    row = r["rows"][0]
    assert (row["name"], row["value"], row["cost"], row["margin"], row["margin_pct"]) == \
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


def test_sales_register_with_fifo_cost_and_a_return(db, books):
    r = analysis.trade_analysis(db, books.id, "sales", *SPAN, by="voucher", cost="fifo")
    line = r["lines"][0]
    assert (line["item"], line["value"], line["cost_per_unit"], line["cost"], line["margin"], line["margin_pct"]) == \
        ("Finished Cooler", "500.00", "200.00", "200.00", "300.00", "60.00")

    # The customer returns it: the sale and its cost both come back out, and stock returns at cost, not at 500.
    engine.create_voucher(db, books, {
        "voucher_type": "Credit Note", "voucher_number": "CN-1", "date": "2026-06-20", "party_ledger": "Acme Traders",
        "entries": [{"ledger": "Acme Traders", "amount": "-500"}, {"ledger": "Sales", "amount": "500"}],
        "inventory": [{"item": "Finished Cooler", "quantity": "1", "rate": "500", "unit": "Nos", "ledger": "Sales"}]},
        USER, action="post", source="tally")
    r = analysis.trade_analysis(db, books.id, "sales", *SPAN, by="item", cost="fifo")
    assert (r["total"]["value"], r["total"]["cost"], r["total"]["margin"]) == ("0.00", "0.00", "0.00")
    cooler = next(i for i in analysis.fifo_costing(db, books.id, *SPAN)["items"] if i["item"] == "Finished Cooler")
    assert (cooler["closing_qty"], cooler["closing_value"], cooler["gross_profit"]) == ("2", "400.00", "0.00")


def test_sales_fifo_cost_api(db, books):
    from app.core.security import hash_key
    db.add(User(username="boss2", api_key_hash=hash_key("k-boss2"), is_superadmin=True))
    db.commit()
    r = TestClient(app).get(f"/api/v1/reports/sales-fifo-cost?company_id={books.id}&from_date=2026-04-01&to_date=2027-03-31",
                            headers={"X-API-Key": "k-boss2"}).json()
    assert r["by"] == "voucher" and r["total"]["cost"] == "200.00" and r["total"]["margin"] == "300.00"
    line = r["lines"][0]
    assert list(line)[list(line).index("item") + 1] == "item_group"
    assert list(line)[list(line).index("value") + 1] == "tax" and "tax" in r["total"]


def test_sales_fifo_cost_excel_export(db, books):
    import io
    import zipfile
    from app.core.security import hash_key
    db.add(User(username="boss3", api_key_hash=hash_key("k-boss3"), is_superadmin=True))
    db.commit()
    r = TestClient(app).get(f"/api/v1/reports/sales-fifo-cost?company_id={books.id}&from_date=2026-04-01&to_date=2027-03-31"
                            "&format=xlsx", headers={"X-API-Key": "k-boss3"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    sheet = zipfile.ZipFile(io.BytesIO(r.content)).read("xl/worksheets/sheet1.xml").decode()
    assert "Item group" in sheet and "FIFO cost" in sheet and "Gross profit" in sheet and ">Total<" in sheet and "voucher_id" not in sheet.lower()


def test_group_wise_sales_rolls_items_up_the_group_tree(db, books):
    from app.models import StockGroup
    top = StockGroup(company_id=books.id, name="FINISHED GOODS")
    db.add(top)
    db.flush()
    sub = StockGroup(company_id=books.id, name="OIL COOLER-ACE", parent_id=top.id)
    db.add(sub)
    db.flush()
    db.query(StockItem).filter_by(company_id=books.id, name="Finished Cooler").one().group_id = sub.id
    db.commit()
    r = analysis.group_wise(db, books.id, "sales", *SPAN)
    [root] = r["tree"]
    [child] = root["children"]
    [item] = child["children"]
    assert (root["name"], child["name"], item["name"]) == ("FINISHED GOODS", "OIL COOLER-ACE", "Finished Cooler")
    assert (root["value"], root["cost"], root["margin"], root["quantity"], root["unit"]) == ("500.00", "200.00", "300.00", "1", "Nos")
    assert r["total"]["value"] == "500.00" and [x["level"] for x in r["rows"]] == [1, 2, 3]
    assert analysis.group_wise(db, books.id, "sales", *SPAN, stock_group=sub.id)["total"]["value"] == "500.00"
    assert analysis.group_wise(db, books.id, "sales", *SPAN, stock_group=uuid.uuid4())["tree"] == []


def test_fifo_closing_rate_on_a_date_is_the_default_rate_of_a_stock_line(db, books):
    def rate(day, item="Component A", **kw):
        return analysis.fifo_rates(db, books.id, date.fromisoformat(day), [item], **kw)[item]
    assert rate("2026-04-01")["rate"] is None                       # nothing bought yet
    assert rate("2026-04-10")["rate"] == "100.00"                   # 10 @ 100
    assert rate("2026-05-10")["rate"] == "110.00"                   # 10 @ 100 + 10 @ 120
    r = rate("2026-06-05")                                          # 4 issued from the oldest lot: 6 @ 100 + 10 @ 120
    assert (r["rate"], r["qty"], r["basis"]) == ("112.50", "16", "FIFO closing")
    assert rate("2026-06-20", "Finished Cooler")["rate"] == "200.00"
    from sqlalchemy import select
    from app.models import Voucher
    mj = db.scalar(select(Voucher.id).where(Voucher.voucher_number == "MJ-1"))
    assert rate("2026-06-05", exclude_voucher=mj)["rate"] == "110.00"   # editing MJ-1: its own issue left out
    from fastapi.testclient import TestClient
    from app.core.security import hash_key
    from app.main import app
    from app.models import User
    db.add(User(username="fifo-boss", api_key_hash=hash_key("k-fifo"), is_superadmin=True))
    db.commit()
    r = TestClient(app).get(f"/api/v1/companies/{books.id}/masters/fifo-rates?as_of=2026-06-05&item=Component%20A&item=Finished%20Cooler",
                            headers={"X-API-Key": "k-fifo"}).json()
    assert r["Component A"]["rate"] == "112.50" and r["Finished Cooler"]["rate"] == "200.00"


def test_fifo_month_by_month(db, books):
    """Bought 10 at 100 in April and 10 at 120 in May; 4 issued in June take the April cost (100)."""
    r = analysis.fifo_monthly(db, books.id, date(2026, 4, 1), date(2026, 6, 30))
    assert [mo["label"] for mo in r["months"]] == ["Apr 2026", "May 2026", "Jun 2026"]
    a = next(x for x in r["items"] if x["item"] == "Component A")
    got = [(mo["opening_qty"], mo["opening_value"], mo["in_qty"], mo["in_value"], mo["out_qty"], mo["out_cost"],
            mo["closing_qty"], mo["closing_value"]) for mo in a["months"]]
    assert got == [("0", "0.00", "10", "1000.00", "0", "0.00", "10", "1000.00"),
                   ("10", "1000.00", "10", "1200.00", "0", "0.00", "20", "2200.00"),
                   ("20", "2200.00", "0", "0.00", "4", "400.00", "16", "1800.00")]
    assert "parent_group" in a
    rates = [(mo["opening_rate"], mo["in_rate"], mo["out_rate"], mo["closing_rate"]) for mo in a["months"]]
    assert rates == [(None, "100.00", None, "100.00"), ("100.00", "120.00", None, "110.00"),
                     ("110.00", None, "100.00", "112.50")]          # June: 4 out at April's 100; 16 left worth 1,800
    # the period figures are those of the FIFO costing report, and each month's closing is the next opening
    whole = next(x for x in analysis.fifo_costing(db, books.id, date(2026, 4, 1), date(2026, 6, 30))["items"]
                 if x["item"] == "Component A")
    assert (a["opening_value"], a["in_value"], a["out_cost"], a["closing_value"]) == \
        (whole["opening_value"], whole["in_value"], whole["out_cost"], whole["closing_value"])
    for item in r["items"]:
        for prev, nxt in zip(item["months"], item["months"][1:]):
            assert (prev["closing_qty"], prev["closing_value"]) == (nxt["opening_qty"], nxt["opening_value"])
    june = r["month_totals"][2]
    assert june["label"] == "Jun 2026" and june["closing_value"] == r["total"]["closing_value"]
    # a period starting in June: its opening is May's closing
    j = next(x for x in analysis.fifo_monthly(db, books.id, date(2026, 6, 1), date(2026, 6, 30))["items"]
             if x["item"] == "Component A")
    assert (j["opening_qty"], j["opening_value"]) == ("20", "2200.00") and len(j["months"]) == 1


def test_fifo_month_by_month_api_and_excel(db, books):
    import io
    import zipfile
    from app.core.security import hash_key
    db.add(User(username="fm-boss", api_key_hash=hash_key("k-fm"), is_superadmin=True))
    db.commit()
    api, h = TestClient(app), {"X-API-Key": "k-fm"}
    q = f"/api/v1/reports/fifo-monthly?company_id={books.id}&from_date=2026-04-01&to_date=2026-06-30"
    j = api.get(q, headers=h).json()
    assert len(j["months"]) == 3 and j["rows"][0]["opening_rate"] is None
    x = api.get(q + "&format=xlsx", headers=h)
    assert x.status_code == 200
    sheet = zipfile.ZipFile(io.BytesIO(x.content)).read("xl/worksheets/sheet1.xml").decode()
    assert "112.50" in sheet or "112.5" in sheet            # June's closing rate of Component A


def test_fifo_month_by_month_shows_the_stock_group_and_its_parent(db, books):
    from app.models import StockGroup
    top = StockGroup(company_id=books.id, name="RAW MATERIAL")
    db.add(top)
    db.flush()
    child = StockGroup(company_id=books.id, name="RAW MATERIAL COOLER", parent_id=top.id)
    db.add(child)
    db.flush()
    leaf = StockGroup(company_id=books.id, name="FAN", parent_id=child.id)      # FAN > RAW MATERIAL COOLER > RAW MATERIAL > Primary
    db.add(leaf)
    db.flush()
    db.query(StockItem).filter_by(company_id=books.id, name="Component A").one().group_id = leaf.id
    db.query(StockItem).filter_by(company_id=books.id, name="Finished Cooler").one().group_id = top.id
    db.commit()
    r = analysis.fifo_monthly(db, books.id, date(2026, 4, 1), date(2026, 6, 30))
    a = next(x for x in r["items"] if x["item"] == "Component A")
    assert (a["stock_group"], a["parent_group"]) == ("FAN", "RAW MATERIAL")       # the group under Primary
    f = next(x for x in r["items"] if x["item"] == "Finished Cooler")
    assert (f["stock_group"], f["parent_group"]) == ("RAW MATERIAL", "RAW MATERIAL")   # itself, when straight under Primary
    row = next(x for x in r["rows"] if x["item"] == "Component A")
    assert list(row)[:3] == ["item", "stock_group", "parent_group"]



def test_store_wise_stock_summary(db, books):
    """Components bought into BASEMENT, 4 moved to the shop floor for production: each store has its own
    quantities; values follow FIFO; the store's group (its parent godown) is shown."""
    from app.models import Godown
    top = Godown(company_id=books.id, name="MAIN STORE")
    db.add(top)
    db.flush()
    mid = Godown(company_id=books.id, name="RAW MATERIAL STORE", parent_id=top.id)   # BASEMENT > RAW MATERIAL STORE > MAIN STORE
    db.add(mid)
    db.flush()
    db.add(Godown(company_id=books.id, name="BASEMENT", parent_id=mid.id))
    db.commit()
    from sqlalchemy import select
    from app.models import Voucher, VoucherInventoryLine
    for line in db.scalars(select(VoucherInventoryLine).join(Voucher).where(Voucher.company_id == books.id)):
        line.godown = "BASEMENT" if line.quantity > 0 and line.stock_item.name == "Component A" else "Shop Floor"
    db.commit()
    r = analysis.godown_summary(db, books.id, date(2026, 4, 1), date(2026, 6, 30))
    by = {(x["store"], x["item"]): x for x in r["rows"]}
    base, floor = by[("BASEMENT", "Component A")], by[("Shop Floor", "Component A")]
    assert (base["in_qty"], base["in_value"], base["out_qty"], base["closing_qty"]) == ("20", "2200.00", "0", "20")
    assert (floor["out_qty"], floor["out_cost"], floor["out_rate"], floor["closing_qty"]) == ("4", "400.00", "100.00", "-4")
    assert (base["store_group"], base["store_parent"]) == ("MAIN STORE", "RAW MATERIAL STORE")
    assert (floor["store_group"], floor["store_parent"]) == ("Shop Floor", "Shop Floor")    # a godown of its own
    assert base["closing_rate"] == "112.50" and base["closing_value"] == "2250.00"    # 16 left worth 1,800: 112.50 each
    whole = next(x for x in analysis.fifo_costing(db, books.id, date(2026, 4, 1), date(2026, 6, 30))["items"]
                 if x["item"] == "Component A")
    stores = [x for x in r["rows"] if x["item"] == "Component A"]
    assert sum(Decimal(x["closing_qty"]) for x in stores) == Decimal(whole["closing_qty"])
    assert sum(Decimal(x["closing_value"]) for x in stores) == Decimal(whole["closing_value"])
    assert sum(Decimal(x["out_cost"]) for x in stores) == Decimal(whole["out_cost"])
