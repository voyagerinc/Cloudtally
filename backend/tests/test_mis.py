"""MIS reports by division: cost centres, shared splits, stock values, deferred expenses."""
import io
import zipfile
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from app.core.security import hash_key
from app.main import app
from app.models import Ledger, StockGroup, StockItem, User
from app.models.mis import DeferredExpense
from app.reports import mis
from app.services.accounting import masters
from app.services.accounting import vouchers as engine
from tests.conftest import USER
from tests.test_analysis import books  # noqa: F401  (fixture: buy, produce and sell a cooler)

JUNE = (date(2026, 6, 1), date(2026, 6, 30))


def _setup(db, books):  # noqa: F811
    rm = StockGroup(company_id=books.id, name="RAW MATERIAL COOLER")
    fg = StockGroup(company_id=books.id, name="FINISHED GOODS OC")
    db.add_all([rm, fg])
    db.flush()
    db.query(StockItem).filter_by(company_id=books.id, name="Component A").one().group_id = rm.id
    db.query(StockItem).filter_by(company_id=books.id, name="Finished Cooler").one().group_id = fg.id
    masters.create_ledger(db, books, {"name": "Factory Power", "group": "Direct Expenses"}, USER)
    masters.create_ledger(db, books, {"name": "Office Rent", "group": "Indirect Expenses"}, USER)
    db.commit()
    engine.create_voucher(db, books, {
        "voucher_type": "Journal", "voucher_number": "J-1", "date": "2026-06-15",
        "entries": [{"ledger": "Factory Power", "amount": "900",
                     "cost": [{"centre": "OIL COOLER", "amount": "600"}, {"centre": "H.O", "amount": "300"}]},
                    {"ledger": "Office Rent", "amount": "300"},
                    {"ledger": "Bharat Supplies", "amount": "-1200"}]}, USER, action="post", source="tally")
    return {led.name: led for led in db.query(Ledger).filter(Ledger.company_id == books.id,
                                                         Ledger.name.in_(("Factory Power", "Office Rent")))}


def test_amounts_follow_cost_centres_then_stock_groups_then_shared_split(db, books):  # noqa: F811
    ledgers = _setup(db, books)
    s = mis.load_settings(db, books.id)
    assert s["cost_centres"] == {"H.O": "shared", "OIL COOLER": "cooler"}
    b = mis.expense_breakup(db, books.id, *JUNE)
    factory = {r["ledger"]: r for r in b["sections"][0]["rows"]}
    admin = {r["ledger"]: r for r in b["sections"][1]["rows"]}
    # 600 on OIL COOLER, 300 on H.O shared in thirds
    assert factory["Factory Power"]["by"] == {"cooler": "700.00", "hydraulic": "100.00", "coc": "100.00"}
    assert admin["Office Rent"]["by"] == {"cooler": "100.00", "hydraulic": "100.00", "coc": "100.00"}

    # a split for the ledger, and a deferred charge of 1,200 over 12 months that follows it
    mis.save_settings(db, books.id, {"ledgers": {str(ledgers["Office Rent"].id): {"split": {"cooler": 50, "hydraulic": 50}}}}, "t")
    db.add(DeferredExpense(company_id=books.id, ledger_id=ledgers["Office Rent"].id, head="Insurance",
                           total_amount=Decimal("1200"), first_month=date(2026, 4, 1), months=12))
    db.commit()
    rent = next(r for r in mis.expense_breakup(db, books.id, *JUNE)["sections"][1]["rows"] if r["ledger"] == "Office Rent")
    assert (rent["tally"], rent["deferred"], rent["amount"]) == ("300.00", "100.00", "400.00")
    assert rent["by"] == {"cooler": "200.00", "hydraulic": "200.00", "coc": "0.00"}
    # "replace": the deferred charge stands instead of what Tally booked
    mis.save_settings(db, books.id, {"ledgers": {str(ledgers["Office Rent"].id): {"split": {"cooler": 50, "hydraulic": 50},
                                                                                   "deferred": "replace"}}}, "t")
    db.commit()
    rent = next(r for r in mis.expense_breakup(db, books.id, *JUNE)["sections"][1]["rows"] if r["ledger"] == "Office Rent")
    assert rent["amount"] == "100.00"
    heads = {r["head"]: r for r in mis.expenses(db, books.id, *JUNE)["sections"][1]["rows"]}
    assert heads["Indirect Expenses"]["total"] == "100.00"


def test_pl_by_division(db, books):  # noqa: F811
    _setup(db, books)
    r = mis.profit_and_loss(db, books.id, *JUNE)
    line = {ln["key"]: ln for sec in r["sections"] for ln in sec["lines"]}
    assert line["sales"]["by"]["cooler"] == "500.00" and line["sales"]["total"] == "500.00"
    # RM: 20 components worth 2,200 at the start of June, 16 left at the average 110 at its end
    assert (line["rm_open"]["by"]["cooler"], line["rm_close"]["by"]["cooler"], line["rm_consumption"]["by"]["cooler"]) == \
        ("2200.00", "1760.00", "440.00")
    assert line["factory"]["by"]["cooler"] == "700.00"
    assert line["fg_close"]["by"]["cooler"] == "200.00"   # one cooler of two made for 400
    cogs = Decimal("0") + Decimal("440") + Decimal("700") - Decimal("200")
    assert Decimal(line["cogs"]["by"]["cooler"]) == cogs
    assert Decimal(line["gross"]["by"]["cooler"]) == Decimal("500") - cogs
    assert Decimal(line["net"]["total"]) == Decimal(line["gross"]["total"]) - Decimal(line["admin"]["total"])
    # a closing value entered by hand for the month replaces the one from stock
    mis.save_settings(db, books.id, {"stock_overrides": {"2026-06": {"cooler": {"fg": {"closing": "300"}}}}}, "t")
    db.commit()
    line = {ln["key"]: ln for sec in mis.profit_and_loss(db, books.id, *JUNE)["sections"] for ln in sec["lines"]}
    assert line["fg_close"]["by"]["cooler"] == "300.00" and line["fg_close"]["overridden"] == ["cooler"]


def test_deferred_schedule_charge_and_balance():
    s = DeferredExpense(head="Software", total_amount=Decimal("1200"), first_month=date(2026, 4, 1), months=12)
    assert mis.deferred_charge(s, date(2026, 3, 1)) == 0 and mis.deferred_charge(s, date(2026, 4, 1)) == 100
    assert mis.deferred_charge(s, date(2027, 3, 1)) == 100 and mis.deferred_charge(s, date(2027, 4, 1)) == 0
    assert mis.deferred_balance(s, date(2026, 3, 1)) == 1200 and mis.deferred_balance(s, date(2026, 10, 1)) == 500
    assert mis.deferred_balance(s, date(2027, 3, 1)) == 0 and mis.deferred_balance(s, date(2026, 1, 1)) == 0


def test_mis_api_settings_deferred_and_excel(db, books):  # noqa: F811
    ledgers = _setup(db, books)
    db.add(User(username="mis-boss", api_key_hash=hash_key("k-mis"), is_superadmin=True))
    db.commit()
    api, h, q = TestClient(app), {"X-API-Key": "k-mis"}, f"company_id={books.id}"
    st = api.get(f"/api/v1/mis/settings?{q}", headers=h).json()
    assert {c["name"]: c["division"] for c in st["cost_centres"]} == {"H.O": "shared", "OIL COOLER": "cooler"}
    assert any(g["name"] == "RAW MATERIAL COOLER" and g["effective_kind"] == "rm" for g in st["stock_groups"])
    r = api.put(f"/api/v1/mis/settings?{q}", json={"cost_centres": {"H.O": "nowhere"}}, headers=h)
    assert r.status_code == 422
    assert api.put(f"/api/v1/mis/settings?{q}", json={"cost_centres": {"H.O": "coc"}}, headers=h).status_code == 200
    rows = api.get(f"/api/v1/reports/mis-exp-breakup?{q}&from_date=2026-06-01&to_date=2026-06-30", headers=h).json()
    power = next(r for r in rows["sections"][0]["rows"] if r["ledger"] == "Factory Power")
    assert power["by"] == {"cooler": "600.00", "hydraulic": "0.00", "coc": "300.00"}

    body = {"head": "Insurance", "ledger_id": str(ledgers["Office Rent"].id), "total_amount": "1200",
            "first_month": "2026-04", "months": 12}
    made = api.post(f"/api/v1/mis/deferred?{q}", json=body, headers=h).json()
    d = api.get(f"/api/v1/reports/mis-deferred?{q}", headers=h)
    assert d.status_code == 200
    assert api.put(f"/api/v1/mis/deferred/{made['id']}?{q}", json={**body, "months": 6}, headers=h).json()["months"] == 6
    rep = api.get(f"/api/v1/reports/mis-deferred?{q}&from_date=2026-04-01&to_date=2027-03-31", headers=h).json()
    assert rep["heads"][0]["charge"][:7] == ["200.00"] * 6 + ["0.00"]
    assert api.delete(f"/api/v1/mis/deferred/{made['id']}?{q}", headers=h).status_code == 204

    x = api.get(f"/api/v1/reports/mis-pl?{q}&from_date=2026-06-01&to_date=2026-06-30&format=xlsx", headers=h)
    assert x.status_code == 200
    sheet = zipfile.ZipFile(io.BytesIO(x.content)).read("xl/worksheets/sheet1.xml").decode()
    assert "Net revenue" in sheet and "Cooler" in sheet
    assert api.get(f"/api/v1/reports/mis-expense?{q}&from_date=2026-06-01", headers=h).json()["to"] == "2026-06-30"


def test_finance_sheet_in_the_workbook_layout(db, books):  # noqa: F811
    _setup(db, books)
    r = mis.finance_sheet(db, books.id, *JUNE)
    assert r["period"] == "Jun-26"
    assert [b["title"] for b in r["blocks"]] == ["RM Store", "Production Cost (WIP)", "Finished Goods Cost", "Sales", "P&L",
                                                 "Net Profit", "GP P&L"]
    rm = r["blocks"][0]
    assert rm["columns"] == ["Opening Stock Value", "Purchase Value", "Closing Stock Value", "RM Consumption"]
    cooler = rm["rows"][0]
    assert (cooler["month"], cooler["category"], cooler["values"]) == ("Jun-26", "Cooler", ["2200.00", "0.00", "1760.00", "440.00"])
    assert cooler["pct"] == "88.00"            # 440 of the 500 net revenue
    assert rm["rows"][-1]["category"] == "Total" and rm["editable"] == {0: "opening:rm", 2: "closing:rm"}
    pl = next(b for b in r["blocks"] if b["title"] == "P&L")
    assert pl["rows"][0]["values"][0] == "500.00"


def test_finance_sheet_excel_has_numbers(db, books):  # noqa: F811
    _setup(db, books)
    db.add(User(username="fs-boss", api_key_hash=hash_key("k-fs"), is_superadmin=True))
    db.commit()
    x = TestClient(app).get(f"/api/v1/reports/mis-finance-sheet?company_id={books.id}&from_date=2026-06-01&to_date=2026-06-30"
                            "&format=xlsx", headers={"X-API-Key": "k-fs"})
    assert x.status_code == 200
    sheet = zipfile.ZipFile(io.BytesIO(x.content)).read("xl/worksheets/sheet1.xml").decode()
    assert "Monthly Finance Sheet - Jun-26" in sheet and "Production Cost (WIP)" in sheet
    assert '<c r="C5" s="2"><v>2200.00</v></c>' in sheet   # a number, formatted, not text


def test_excel_headings_are_bold_on_yellow(db, books):  # noqa: F811
    from app.reports import xlsx
    _setup(db, books)
    db.add(User(username="y-boss", api_key_hash=hash_key("k-y"), is_superadmin=True))
    db.commit()
    x = TestClient(app).get(f"/api/v1/reports/mis-pl?company_id={books.id}&from_date=2026-06-01&to_date=2026-06-30"
                            "&format=xlsx", headers={"X-API-Key": "k-y"})
    book = zipfile.ZipFile(io.BytesIO(x.content))
    sheet = book.read("xl/worksheets/sheet1.xml").decode()
    styles = book.read("xl/styles.xml").decode()
    assert 'rgb="FFFFFF00"' in styles                                  # the yellow fill
    head = f's="{xlsx.HEAD}"'
    assert f'<c r="A3" t="inlineStr" {head}><is><t xml:space="preserve">Particulars' in sheet
    assert f'<c r="A4" t="inlineStr" {head}><is><t xml:space="preserve">Sales' in sheet   # each group has a heading
    assert f'<c r="B4" {head}/>' in sheet                              # ... banded across the table
    assert "% of net revenue" in sheet


def test_stock_journal_shows_in_and_out_separately_with_item_group(db, books):  # noqa: F811
    from app.models import Voucher
    _setup(db, books)
    db.add(User(username="v-boss", api_key_hash=hash_key("k-v"), is_superadmin=True))
    db.commit()
    mj = db.query(Voucher).filter_by(company_id=books.id, voucher_number="MJ-1").one()
    api, h = TestClient(app), {"X-API-Key": "k-v"}
    groups = api.get(f"/api/v1/vouchers/{mj.id}", headers=h).json()["item_groups"]
    assert groups == {"Component A": "RAW MATERIAL COOLER", "Finished Cooler": "FINISHED GOODS OC"}
    x = api.get(f"/api/v1/vouchers/{mj.id}/xlsx", headers=h)
    sheet = zipfile.ZipFile(io.BytesIO(x.content)).read("xl/worksheets/sheet1.xml").decode()
    i_in, i_out = sheet.index("In (produced / received)"), sheet.index("Out (consumed / issued)")
    assert i_in < sheet.index("Finished Cooler") < i_out < sheet.index("Component A")
    assert sheet.index(">Item<") < sheet.index(">Item group<") and "Total in" in sheet and "Total out" in sheet


def test_stock_journal_register_side_by_side(db, books):  # noqa: F811
    from app.reports import analysis
    _setup(db, books)
    r = analysis.stock_journal_register(db, books.id, date(2026, 4, 1), date(2027, 3, 31), voucher_type="all")
    [v] = r["vouchers"]
    assert (v["voucher_number"], [x["item"] for x in v["in"]], [x["item"] for x in v["out"]]) == ("MJ-1", ["Finished Cooler"], ["Component A"])
    assert v["in"][0]["item_group"] == "FINISHED GOODS OC" and v["out_total"] == {"quantity": "4", "unit": "Nos", "amount": "400.00", "items": 1}
    db.add(User(username="sj-boss", api_key_hash=hash_key("k-sj"), is_superadmin=True))
    db.commit()
    x = TestClient(app).get(f"/api/v1/reports/stock-journal?company_id={books.id}&from_date=2026-04-01&to_date=2027-03-31"
                            "&voucher_type=all&format=xlsx", headers={"X-API-Key": "k-sj"})
    sheet = zipfile.ZipFile(io.BytesIO(x.content)).read("xl/worksheets/sheet1.xml").decode()
    # In and Out side by side on the same row: the In heading in column A, the Out heading in column I
    import re
    row_in = re.search(r'r="A(\d+)" t="inlineStr" s="4"><is><t xml:space="preserve">In \(produced / received\) 1 item', sheet)
    row_out = re.search(r'r="I(\d+)" t="inlineStr" s="4"><is><t xml:space="preserve">Out \(consumed / issued\) 1 item', sheet)
    assert row_in and row_out and row_in.group(1) == row_out.group(1)


def test_material_transfer_to_wip_report(db, books):  # noqa: F811
    from app.models import Voucher, VoucherType
    _setup(db, books)
    mt = VoucherType(company_id=books.id, name="Material Transfer to WIP", base_type="Stock Journal", affects_stock=True)
    db.add(mt)
    db.flush()
    db.query(Voucher).filter_by(company_id=books.id, voucher_number="MJ-1").one().voucher_type_id = mt.id
    db.add(User(username="mt-boss", api_key_hash=hash_key("k-mt"), is_superadmin=True))
    db.commit()
    api, h = TestClient(app), {"X-API-Key": "k-mt"}
    q = f"company_id={books.id}&from_date=2026-04-01&to_date=2027-03-31"
    r = api.get(f"/api/v1/reports/material-transfer-wip?{q}", headers=h).json()
    assert r["voucher_type"] == "Material Transfer to WIP" and r["total"]["vouchers"] == 1
    x = api.get(f"/api/v1/reports/material-transfer-wip?{q}&format=xlsx", headers=h)
    sheet = zipfile.ZipFile(io.BytesIO(x.content)).read("xl/worksheets/sheet1.xml").decode()
    assert ">2 Nos<" in sheet and ">4 Nos<" in sheet          # quantity with its unit, as on the sheet
    assert '<c r="H4" t="inlineStr" s="4"><is><t xml:space="preserve">Out (consumed / issued) 1 item' in sheet


def test_wo_manufacturing_report(db, books):  # noqa: F811
    from app.models import Voucher, VoucherType
    _setup(db, books)
    wo = VoucherType(company_id=books.id, name="WO Manufacturing Vouchers", base_type="Stock Journal", affects_stock=True)
    db.add(wo)
    db.flush()
    db.query(Voucher).filter_by(company_id=books.id, voucher_number="MJ-1").one().voucher_type_id = wo.id
    db.add(User(username="wo-boss", api_key_hash=hash_key("k-wo"), is_superadmin=True))
    db.commit()
    api, h = TestClient(app), {"X-API-Key": "k-wo"}
    q = f"company_id={books.id}&from_date=2026-04-01&to_date=2027-03-31"
    r = api.get(f"/api/v1/reports/wo-manufacturing?{q}", headers=h).json()
    [v] = r["vouchers"]
    assert (v["in"][0]["item"], v["in"][0]["quantity"], v["in"][0]["item_group"]) == ("Finished Cooler", "2", "FINISHED GOODS OC")
    [g] = v["consumed_groups"]
    assert (g["group"], [x["item"] for x in g["items"]], g["amount"]) == ("RAW MATERIAL COOLER", ["Component A"], "400.00")
    assert r["rows"][0]["produced_item"] == "Finished Cooler" and r["rows"][0]["consumed_item_group"] == "RAW MATERIAL COOLER"
    x = api.get(f"/api/v1/reports/wo-manufacturing?{q}&format=xlsx", headers=h)
    sheet = zipfile.ZipFile(io.BytesIO(x.content)).read("xl/worksheets/sheet1.xml").decode()
    assert "Total RAW MATERIAL COOLER" in sheet and ">Finished Cooler<" in sheet and ">2 Nos<" in sheet
    assert '<c r="E3" t="inlineStr" s="4"><is><t xml:space="preserve">Batch' in sheet
    assert '<c r="A3" t="inlineStr" s="4"><is><t xml:space="preserve">Voucher no' in sheet   # voucher number before the date
    assert '<c r="B3" t="inlineStr" s="4"><is><t xml:space="preserve">Voucher type' in sheet
    assert '<c r="O3" t="inlineStr" s="4"><is><t xml:space="preserve">OC' in sheet
    assert r["total"]["divisions"]["cooler"] == "400.00"        # RAW MATERIAL COOLER is the OC (cooler) division
    assert '<c r="H3" t="inlineStr" s="4"><is><t xml:space="preserve">Consumed item group' in sheet


def test_material_transfer_main_group(db, books):  # noqa: F811
    from app.models import StockGroup, Voucher, VoucherType
    _setup(db, books)
    top = StockGroup(company_id=books.id, name="FINISHED GOODS")
    db.add(top)
    db.flush()
    db.query(StockGroup).filter_by(company_id=books.id, name="FINISHED GOODS OC").one().parent_id = top.id
    mt = VoucherType(company_id=books.id, name="Material Transfer to WIP", base_type="Stock Journal", affects_stock=True)
    db.add(mt)
    db.flush()
    db.query(Voucher).filter_by(company_id=books.id, voucher_number="MJ-1").one().voucher_type_id = mt.id
    db.add(User(username="mtm-boss", api_key_hash=hash_key("k-mtm"), is_superadmin=True))
    db.commit()
    api, h = TestClient(app), {"X-API-Key": "k-mtm"}
    q = f"company_id={books.id}&from_date=2026-04-01&to_date=2027-03-31"
    [v] = api.get(f"/api/v1/reports/material-transfer-wip-main?{q}", headers=h).json()["vouchers"]
    assert (v["in"][0]["item_group"], v["in"][0]["main_group"]) == ("FINISHED GOODS OC", "FINISHED GOODS")
    assert v["out"][0]["main_group"] == "RAW MATERIAL COOLER"     # a group straight under Primary is its own main group
    x = api.get(f"/api/v1/reports/material-transfer-wip-main?{q}&format=xlsx", headers=h)
    sheet = zipfile.ZipFile(io.BytesIO(x.content)).read("xl/worksheets/sheet1.xml").decode()
    assert '<c r="C5" t="inlineStr" s="4"><is><t xml:space="preserve">Main group' in sheet
    assert '<c r="I4" t="inlineStr" s="4"><is><t xml:space="preserve">Out (consumed / issued) 1 item' in sheet


def test_any_table_downloads_as_excel(db, books):  # noqa: F811
    db.add(User(username="xl-boss", api_key_hash=hash_key("k-xl"), is_superadmin=True))
    db.commit()
    body = {"title": "Trial balance - Nov", "widths": [30, 14],
            "rows": [[{"v": "Particulars", "s": "head"}, {"v": "Amount", "s": "head"}],
                     [{"v": "   Cash"}, {"v": "-1234.50", "s": "num"}],
                     [{"v": "Total", "s": "bold"}, {"v": "2 Nos", "s": "boldnum"}], [None, "text"]]}
    x = TestClient(app).post(f"/api/v1/export/table?company_id={books.id}", json=body, headers={"X-API-Key": "k-xl"})
    assert x.status_code == 200 and 'filename="Trial balance - Nov.xlsx"' in x.headers["content-disposition"]
    sheet = zipfile.ZipFile(io.BytesIO(x.content)).read("xl/worksheets/sheet1.xml").decode()
    assert '<c r="A1" t="inlineStr" s="4"><is><t xml:space="preserve">Particulars' in sheet   # heading bold on yellow
    assert '<c r="B2" s="2"><v>-1234.50</v></c>' in sheet                                     # amount as a number
    assert '<c r="B3" t="inlineStr" s="1"><is><t xml:space="preserve">2 Nos' in sheet        # not a number: bold text
    assert '>   Cash<' in sheet                                                                 # tree level kept


BOM_XML = """<STOCKITEM NAME="AH 0608-C-F-N-024-DC-01"><PARENT>OIL COOLER-ACE</PARENT>
 <MULTICOMPONENTLIST.LIST><COMPONENTLISTNAME>AH0608CF024DN</COMPONENTLISTNAME><COMPONENTBASICQTY> 1 Nos</COMPONENTBASICQTY>
  <MULTICOMPONENTITEMLIST.LIST><NATUREOFITEM>Component</NATUREOFITEM><STOCKITEMNAME>Radiator AH-0608 (ACE-020)</STOCKITEMNAME>
   <GODOWNNAME>BASEMENT</GODOWNNAME><ACTUALQTY> 1 Nos</ACTUALQTY></MULTICOMPONENTITEMLIST.LIST>
  <MULTICOMPONENTITEMLIST.LIST><NATUREOFITEM>Component</NATUREOFITEM><STOCKITEMNAME>Plain Washer 4X8MM C</STOCKITEMNAME>
   <GODOWNNAME>BASEMENT</GODOWNNAME><ACTUALQTY> 2 Nos</ACTUALQTY></MULTICOMPONENTITEMLIST.LIST>
 </MULTICOMPONENTLIST.LIST></STOCKITEM>"""


def test_bom_is_read_from_tally_and_listed_with_excel(db, books):  # noqa: F811
    from lxml import etree
    from app.connectors.tally import codec
    [bom] = codec.parse_bom(etree.fromstring(BOM_XML))
    assert (bom["name"], bom["basic_qty"], bom["unit"]) == ("AH0608CF024DN", "1", "Nos")
    assert [(c["item"], c["qty"], c["unit"], c["godown"]) for c in bom["components"]] == [
        ("Radiator AH-0608 (ACE-020)", "1", "Nos", "BASEMENT"), ("Plain Washer 4X8MM C", "2", "Nos", "BASEMENT")]
    assert codec.parse_bom(etree.fromstring("<STOCKITEM NAME='x'/>")) is None
    _setup(db, books)
    fg = db.query(StockItem).filter_by(company_id=books.id, name="Finished Cooler").one()
    fg.bom = [{"name": "Std", "basic_qty": "1", "unit": "Nos",
               "components": [{"nature": "Component", "item": "Component A", "godown": "Main", "qty": "2", "unit": "Nos"}]}]
    db.add(User(username="bom-boss", api_key_hash=hash_key("k-bom"), is_superadmin=True))
    db.commit()
    api, h = TestClient(app), {"X-API-Key": "k-bom"}
    [row] = api.get(f"/api/v1/companies/{books.id}/masters/bom", headers=h).json()
    assert row["item"] == "Finished Cooler" and row["item_group"] == row["primary_group"] == "FINISHED GOODS OC"
    assert row["boms"][0]["components"][0]["item_group"] == "RAW MATERIAL COOLER"
    assert api.get(f"/api/v1/companies/{books.id}/masters/bom?q=component%20a", headers=h).json()   # found by component
    assert api.get(f"/api/v1/companies/{books.id}/masters/bom?q=nothing", headers=h).json() == []
    x = api.get(f"/api/v1/companies/{books.id}/masters/bom/xlsx", headers=h)
    sheet = zipfile.ZipFile(io.BytesIO(x.content)).read("xl/worksheets/sheet1.xml").decode()
    assert '<c r="C4" t="inlineStr" s="4"><is><t xml:space="preserve">Primary group</t></is></c>' in sheet and '<c r="F4" t="inlineStr" s="4"><is><t xml:space="preserve">Component' in sheet and ">Component A<" in sheet


def test_coolace_sheet_charges_material_to_the_division_that_used_it(db, books):  # noqa: F811
    """A customised cooler built from a cooler: the cooler's value is in Customised Cooler's cost, and out of
    Cooler's; the company's totals stay the same."""
    _setup(db, books)
    coc = StockGroup(company_id=books.id, name="FINISHED GOODS- COC")
    db.add(coc)
    db.flush()
    db.add(StockItem(company_id=books.id, name="Custom Cooler", group_id=coc.id, unit="Nos"))
    db.commit()
    engine.create_voucher(db, books, {            # one of the two coolers made (200 each) goes into a customised one
        "voucher_type": "Stock Journal", "voucher_number": "WO-1", "date": "2026-06-20", "entries": [],
        "inventory": [{"item": "Finished Cooler", "quantity": "-1", "rate": "200", "amount": "200", "unit": "Nos"},
                      {"item": "Custom Cooler", "quantity": "1", "rate": "200", "amount": "200", "unit": "Nos"}]},
        USER, action="post", source="tally")
    old = {ln["key"]: ln for s in mis.profit_and_loss(db, books.id, *JUNE)["sections"] for ln in s["lines"]}
    r = mis.profit_and_loss(db, books.id, *JUNE, transfers=True)
    new = {ln["key"]: ln for s in r["sections"] for ln in s["lines"]}
    assert r["transfers"] == [{"from": "Cooler", "to": "Customised Cooler", "from_key": "cooler", "to_key": "coc",
                               "kind": "fg", "value": "200.00", "vouchers": 1}]
    assert new["rm_in"]["by"]["coc"] == "200.00" and new["rm_out"]["by"]["cooler"] == "-200.00"
    assert new["rm_in"]["total"] == "200.00" and new["rm_out"]["total"] == "-200.00"     # received = given
    assert Decimal(new["cogs"]["by"]["cooler"]) == Decimal(old["cogs"]["by"]["cooler"]) - 200
    assert Decimal(new["cogm"]["by"]["coc"]) == Decimal(old["cogm"]["by"]["coc"]) + 200
    for key in ("cogs", "gross", "net"):
        assert new[key]["total"] == old[key]["total"]
    sheet = mis.finance_sheet(db, books.id, *JUNE, transfers=True)
    rm = sheet["blocks"][0]
    assert rm["columns"] == ["Opening Stock Value", "Purchase Value", "From Cooler", "From Hydraulic",
                             "From Customised Cooler", "To Other Divisions", "Closing Stock Value", "RM Consumption"]
    coc_row = next(x for x in rm["rows"] if x.get("division") == "coc")
    assert coc_row["values"][2] == "200.00" and coc_row["values"][3] == "0.00"      # from Cooler, not Hydraulic
    assert rm["drill"][2] == {"src": "cooler", "dst": "row"} and rm["drill"][5] == {"src": "row"}
    total = rm["rows"][-1]["values"]
    assert sum(Decimal(v) for v in total[2:5]) == -Decimal(total[5])              # From ... = To Other Divisions
    assert all("Other Divisions" not in c for b in sheet["blocks"][1:] for c in b["columns"])
    # a click on the figure: the voucher and item behind it
    db.add(User(username="dr-boss", api_key_hash=hash_key("k-dr"), is_superadmin=True))
    db.commit()
    d = TestClient(app).get(f"/api/v1/reports/mis-transfers?company_id={books.id}&from_date=2026-06-01&to_date=2026-06-30"
                            "&src=cooler&dst=coc", headers={"X-API-Key": "k-dr"}).json()
    assert d["vouchers"] == 1 and d["total"] == "200.00"
    ln = d["lines"][0]
    assert (ln["voucher_number"], ln["item"], ln["quantity"], ln["value"], ln["produced"], ln["kind"]) ==         ("WO-1", "Finished Cooler", "1", "200.00", ["Custom Cooler"], "fg")
    assert TestClient(app).get(f"/api/v1/reports/mis-transfers?company_id={books.id}&from_date=2026-06-01"
                               "&to_date=2026-06-30&src=hydraulic", headers={"X-API-Key": "k-dr"}).json()["lines"] == []
    # the plain MIS sheet is as before
    assert mis.finance_sheet(db, books.id, *JUNE)["blocks"][0]["columns"][2] == "Closing Stock Value"


def test_coolace_sheet_api_and_excel(db, books):  # noqa: F811
    _setup(db, books)
    db.add(User(username="co-boss", api_key_hash=hash_key("k-co"), is_superadmin=True))
    db.commit()
    api, h = TestClient(app), {"X-API-Key": "k-co"}
    q = f"company_id={books.id}&from_date=2026-06-01&to_date=2026-06-30"
    j = api.get(f"/api/v1/reports/mis-finance-sheet-coolace?{q}", headers=h)
    assert j.status_code == 200 and j.json()["transfers"] == []
    x = api.get(f"/api/v1/reports/mis-finance-sheet-coolace?{q}&format=xlsx", headers=h)
    assert x.status_code == 200
    sheet = zipfile.ZipFile(io.BytesIO(x.content)).read("xl/worksheets/sheet1.xml").decode()
    assert "Material used by another division" in sheet and "From Cooler" in sheet


def test_monthly_sheet_for_every_company_by_category_else_by_stock_group(db, books):  # noqa: F811
    """MIS: Monthly finance sheet (P&L) needs no MIS set-up: a row per stock item category, or per stock group
    when the company keeps no categories; company totals are the same either way."""
    _setup(db, books)
    s = mis.category_settings(db, books.id, *JUNE)
    assert s["basis"] == "category"
    names = [d["name"] for d in s["divisions"]]
    assert "FINISH GOODS" in names and "(No category)" in names
    r = mis.finance_sheet(db, books.id, *JUNE, settings=s)
    assert r["basis"] == "category" and all(not b["editable"] for b in r["blocks"])
    sales = next(b for b in r["blocks"] if b["title"] == "Sales")
    by = {row["category"]: row["values"][0] for row in sales["rows"]}
    assert by["FINISH GOODS"] == "500.00" and by["Total"] == "500.00"        # the cooler's sale, in its category
    net = next(b for b in r["blocks"] if b["title"] == "Net Profit")["rows"][-1]["values"][-1]
    # no categories: the stock groups are the rows, and the company's net profit does not change
    db.query(StockItem).filter_by(company_id=books.id).update({"category": None})
    db.commit()
    g = mis.category_settings(db, books.id, *JUNE)
    assert g["basis"] == "group" and {"RAW MATERIAL COOLER", "FINISHED GOODS OC"} <= {d["name"] for d in g["divisions"]}
    kinds = {db.get(StockItem, iid).name: k for iid, (_, k) in g["_item_dk"].items()}
    assert kinds["Component A"] == "rm" and kinds["Finished Cooler"] == "fg"
    r2 = mis.finance_sheet(db, books.id, *JUNE, settings=g)
    assert next(b for b in r2["blocks"] if b["title"] == "Net Profit")["rows"][-1]["values"][-1] == net
    db.add(User(username="ms-boss", api_key_hash=hash_key("k-ms"), is_superadmin=True))
    db.commit()
    api, h = TestClient(app), {"X-API-Key": "k-ms"}
    url = f"/api/v1/reports/mis-monthly-sheet?company_id={books.id}&from_date={JUNE[0]}&to_date={JUNE[1]}"
    assert api.get(url, headers=h).json()["basis"] == "group"
    assert api.get(url + "&format=xlsx", headers=h).content[:2] == b"PK"
    labels = {p["key"]: p["label"] for grp in api.get("/api/v1/permissions", headers=h).json()["groups"] for p in grp["permissions"]}
    assert labels["report.mis-monthly-sheet"] == "MIS: Monthly finance sheet (P&L)"
    assert labels["report.mis-finance-sheet"] == "Coolace MIS: Monthly finance sheet (P&L)"
