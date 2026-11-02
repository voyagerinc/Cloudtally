"""A stand-in TallyPrime XML server for tests, demos and connector development.

    python -m tools.mock_tally --port 9100

It mimics the parts of Tally's XML-over-HTTP interface that the connector uses: collection
exports with $AlterID/$MasterID/$RemoteID/$Name filters and date ranges, voucher
Create/Alter/Cancel/Delete, ledger Create/Alter, IMPORTRESULT responses and LINEERRORs.
It is NOT Tally: behaviour that must be proven against real TallyPrime is listed in docs/PHASE0.md.
"""
import argparse
import copy
import operator
import re
import threading
import uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from lxml import etree

DEFAULT_GROUPS = [
    ("Capital Account", None, False, False), ("Current Assets", None, False, True),
    ("Current Liabilities", None, False, False), ("Sales Accounts", None, True, False),
    ("Purchase Accounts", None, True, True), ("Direct Expenses", None, True, True),
    ("Indirect Expenses", None, True, True), ("Indirect Incomes", None, True, False),
    ("Fixed Assets", None, False, True), ("Loans (Liability)", None, False, False),
    ("Cash-in-Hand", "Current Assets", False, True), ("Bank Accounts", "Current Assets", False, True),
    ("Sundry Debtors", "Current Assets", False, True), ("Sundry Creditors", "Current Liabilities", False, False),
    ("Duties & Taxes", "Current Liabilities", False, False), ("Stock-in-Hand", "Current Assets", False, True),
]
VOUCHER_TYPES = ["Contra", "Payment", "Receipt", "Journal", "Sales", "Purchase", "Debit Note", "Credit Note",
                 "Memorandum", "Reversing Journal", "Stock Journal", "Sales Order", "Purchase Order", "Material Out"]


def _yn(flag: bool) -> str:
    return "Yes" if flag else "No"


COMPARE = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le, "=": operator.eq}


class MockCompany:
    def __init__(self, name: str):
        self.name = name
        self.guid = str(uuid.uuid4())
        self.alter_id = 1000
        self.master_id = 0
        self.groups: dict[str, dict] = {}
        self.ledgers: dict[str, dict] = {}
        self.vtypes: dict[str, dict] = {}
        self.items: dict[str, dict] = {}
        self.stock_groups: dict[str, dict] = {}
        self.vouchers: dict[int, etree._Element] = {}
        for name_, parent, rev, pos in DEFAULT_GROUPS:
            self.groups[name_] = self._master(name_, parent=parent, isrevenue=rev, isdeemedpositive=pos)
        for vt in VOUCHER_TYPES:
            self.vtypes[vt] = self._master(vt, parent=vt)
        self.add_ledger("Cash", "Cash-in-Hand")

    def alt_vch_id(self) -> int:
        return max((int(v.findtext("ALTERID")) for v in self.vouchers.values()), default=0)

    def alt_mst_id(self) -> int:
        return max((r["alter_id"] for store in (self.groups, self.ledgers, self.vtypes, self.items, self.stock_groups)
                    for r in store.values()), default=0)

    def _next_alter(self) -> int:
        self.alter_id += 1
        return self.alter_id

    def _master(self, name, **fields) -> dict:
        self.master_id += 1
        return {"name": name, "master_id": self.master_id, "guid": f"{self.guid}-{self.master_id:08x}",
                "alter_id": self._next_alter(), **fields}

    def add_ledger(self, name, parent, opening="0.00", **extra) -> dict:
        self.ledgers[name] = self._master(name, parent=parent, openingbalance=opening, **extra)
        return self.ledgers[name]

    def add_item(self, name, unit="Nos", parent=None, **extra) -> dict:
        self.items[name] = self._master(name, parent=parent, baseunits=unit, **extra)
        return self.items[name]

    def add_stock_group(self, name, parent=None, **extra) -> dict:
        self.stock_groups[name] = self._master(name, parent=parent, **extra)
        return self.stock_groups[name]

    # ---------------------------------------------------------------- simulated Tally-side edits

    def edit_voucher(self, master_id: int, fn) -> etree._Element:
        """Simulate a user altering a voucher inside Tally: fn mutates the element."""
        el = self.vouchers[master_id]
        fn(el)
        el.find("ALTERID").text = f" {self._next_alter()}"
        return el

    def find_by_number(self, vtype: str, number: str) -> etree._Element | None:
        return next((v for v in self.vouchers.values()
                     if v.findtext("VOUCHERTYPENAME") == vtype and v.findtext("VOUCHERNUMBER") == number), None)


class MockTally:
    def __init__(self):
        self.companies: dict[str, MockCompany] = {}
        self.lock = threading.RLock()
        self.fail_mode: str | None = None       # refuse | drop_after_write | error
        self.requests: list[str] = []
        self._server: ThreadingHTTPServer | None = None

    def add_company(self, name: str) -> MockCompany:
        self.companies[name] = MockCompany(name)
        return self.companies[name]

    # ---------------------------------------------------------------- server

    def start(self, port: int = 0, host: str = "127.0.0.1") -> int:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self._send("<RESPONSE>TallyPrime Server is Running</RESPONSE>")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
                mock.requests.append(body)
                if mock.fail_mode == "refuse":
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                with mock.lock:
                    response = mock.handle(body)
                if mock.fail_mode == "drop_after_write" and "<TALLYREQUEST>Import" in body:
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                self._send(response)

            def _send(self, text: str):
                data = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/xml; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self._server.server_address[1]

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    # ---------------------------------------------------------------- protocol

    def handle(self, body: str) -> str:
        root = etree.fromstring(body.encode("utf-8"))
        req = (root.findtext(".//TALLYREQUEST") or "").strip().lower()
        company_name = root.findtext(".//SVCURRENTCOMPANY")
        if req == "export":
            return self._export(root, company_name)
        if req in ("import", "import data"):
            company = self.companies.get(company_name or "")
            if company is None:
                return self._result(errors=[f"Could not find Company '{company_name}'"])
            return self._import(root, company)
        return "<RESPONSE>Unknown Request, cannot be processed</RESPONSE>"

    def _envelope(self, inner: str, company: MockCompany | None) -> str:
        counts = ""
        if company:
            counts = (f"<CMPINFO><COMPANY>0</COMPANY><GROUP>{len(company.groups)}</GROUP>"
                      f"<LEDGER>{len(company.ledgers)}</LEDGER><VOUCHER>{len(company.vouchers)}</VOUCHER></CMPINFO>")
        return (f"<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DESC>{counts}</DESC>"
                f"<DATA><COLLECTION>{inner}</COLLECTION></DATA></BODY></ENVELOPE>")

    def _export(self, root, company_name) -> str:
        ctype = (root.findtext(".//COLLECTION/TYPE") or "").strip()
        formula = root.findtext(".//SYSTEM") or ""
        if ctype == "Company":
            inner = "".join(
                f'<COMPANY NAME="{escape(c.name)}"><NAME>{escape(c.name)}</NAME><GUID>{c.guid}</GUID>'
                f"<STARTINGFROM>20260401</STARTINGFROM><BOOKSFROM>20260401</BOOKSFROM>"
                f"<ALTVCHID> {c.alt_vch_id()}</ALTVCHID><ALTMSTID> {c.alt_mst_id()}</ALTMSTID></COMPANY>"
                for c in self.companies.values())
            return self._envelope(inner, self.companies.get(company_name or ""))
        company = self.companies.get(company_name or "")
        if company is None:
            return self._envelope("", None).replace("<DATA>", "<DATA><LINEERROR>Company not open</LINEERROR>")

        def keep(alter_id: int, master_id: int, remote_id: str | None, name: str | None) -> bool:
            values = {"AlterID": alter_id, "MasterID": master_id, "RemoteID": remote_id, "Name": name}
            for clause in filter(None, (c.strip() for c in formula.split(" AND "))):
                m = re.fullmatch(r'\$(\w+) (>=|<=|>|<|=) (?:"([^"]*)"|(-?\d+))', clause)
                if not m:
                    raise ValueError(f"mock cannot evaluate {clause!r}")
                have = values.get(m.group(1))
                want = m.group(3) if m.group(3) is not None else int(m.group(4))
                if have is None or not COMPARE[m.group(2)](have, want):
                    return False
            return True

        parts = []
        if ctype == "Voucher":
            # Like real TallyPrime: SVFROMDATE/SVTODATE do not limit a Voucher collection.
            for mid, el in sorted(company.vouchers.items()):
                if keep(int(el.findtext("ALTERID")), mid, el.findtext("REMOTEID"), None):
                    parts.append(etree.tostring(el, encoding="unicode"))
        else:
            store = {"Group": company.groups, "Ledger": company.ledgers, "Voucher Type": company.vtypes,
                     "Stock Item": company.items, "Stock Group": company.stock_groups}.get(ctype, {})
            tag = ctype.upper().replace(" ", "")
            for rec in store.values():
                if not keep(rec["alter_id"], rec["master_id"], None, rec["name"]):
                    continue
                fields = "".join(
                    f"<{k.upper()}>{escape(_yn(v) if isinstance(v, bool) else str(v))}</{k.upper()}>"
                    for k, v in rec.items() if k not in ("name", "master_id", "guid", "alter_id", "xml") and v is not None)
                fields += rec.get("xml") or ""   # nested lists (address, GST, bank, MSME), verbatim
                if ctype == "Ledger":
                    fields += f"<CLOSINGBALANCE>{self._closing(company, rec)}</CLOSINGBALANCE>"
                parts.append(f'<{tag} NAME="{escape(rec["name"])}" RESERVEDNAME=""><GUID>{rec["guid"]}</GUID>'
                             f"<ALTERID> {rec['alter_id']}</ALTERID><MASTERID> {rec['master_id']}</MASTERID>"
                             f"{fields}{'' if rec.get('parent') else '<PARENT>&#4; Primary</PARENT>'}</{tag}>")
        return self._envelope("".join(parts), company)

    def _closing(self, company: MockCompany, ledger: dict) -> str:
        total = Decimal(ledger.get("openingbalance") or "0")
        for el in company.vouchers.values():
            for e in self._all_entries(el):
                if e[0] == ledger["name"]:
                    total += e[1]
        return f"{total:.2f}"

    @staticmethod
    def _all_entries(el) -> list[tuple[str, Decimal]]:
        out = []
        for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
            for e in el.findall(tag):
                out.append((e.findtext("LEDGERNAME"), Decimal(e.findtext("AMOUNT") or "0")))
        for inv in el.findall("ALLINVENTORYENTRIES.LIST"):
            for a in inv.findall("ACCOUNTINGALLOCATIONS.LIST"):
                out.append((a.findtext("LEDGERNAME"), Decimal(a.findtext("AMOUNT") or "0")))
        return out

    @staticmethod
    def _result(created=0, altered=0, deleted=0, cancelled=0, last_vch_id=0, errors=()) -> str:
        line_errors = "".join(f"<LINEERROR>{escape(e)}</LINEERROR>" for e in errors)
        return ("<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DATA>"
                f"{line_errors}<IMPORTRESULT><CREATED>{created}</CREATED><ALTERED>{altered}</ALTERED>"
                f"<DELETED>{deleted}</DELETED><LASTVCHID>{last_vch_id}</LASTVCHID><LASTMID>0</LASTMID>"
                f"<COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>{len(errors)}</ERRORS>"
                f"<CANCELLED>{cancelled}</CANCELLED></IMPORTRESULT></DATA></BODY></ENVELOPE>")

    def _locate(self, company: MockCompany, el) -> etree._Element | None:
        tagname, value = (el.get("TAGNAME") or "").upper(), el.get("TAGVALUE")
        if tagname == "MASTERID":
            return company.vouchers.get(int(value))
        if tagname == "GUID":
            return next((v for v in company.vouchers.values() if v.findtext("GUID") == value), None)
        if el.get("REMOTEID"):
            return next((v for v in company.vouchers.values() if v.findtext("REMOTEID") == el.get("REMOTEID")), None)
        return None

    def _validate(self, company: MockCompany, el) -> list[str]:
        errors = []
        vtype = el.findtext("VOUCHERTYPENAME") or el.get("VCHTYPE")
        if vtype not in company.vtypes:
            errors.append(f"Voucher Type '{vtype}' does not exist!")
        entries = self._all_entries(el)
        for name, _ in entries:
            if name not in company.ledgers:
                errors.append(f"Ledger '{name}' does not exist!")
        for inv in el.findall("ALLINVENTORYENTRIES.LIST"):
            if inv.findtext("STOCKITEMNAME") not in company.items:
                errors.append(f"Stock Item '{inv.findtext('STOCKITEMNAME')}' does not exist!")
        if sum((a for _, a in entries), Decimal("0")) != 0:
            errors.append("Voucher totals do not match!")
        return errors

    def _import(self, root, company: MockCompany) -> str:
        created = altered = deleted = cancelled = last = 0
        errors: list[str] = []
        for el in root.iter("VOUCHER", "LEDGER"):
            action = (el.get("ACTION") or "Create").capitalize()
            if el.tag == "LEDGER":
                name = el.findtext("NAME.LIST/NAME") or el.get("NAME")
                parent = el.findtext("PARENT")
                if parent not in company.groups:
                    errors.append(f"Group '{parent}' does not exist!")
                    continue
                extra = {c.tag.lower(): (c.text or "").strip() for c in el
                         if isinstance(c.tag, str) and not len(c) and c.tag not in ("PARENT", "OPENINGBALANCE")}
                extra["isbillwiseon"] = el.findtext("ISBILLWISEON") == "Yes"
                extra["xml"] = "".join(etree.tostring(c, encoding="unicode") for c in el
                                       if isinstance(c.tag, str) and len(c) and c.tag != "NAME.LIST")
                if action == "Alter" and el.get("NAME") in company.ledgers:
                    rec = company.ledgers.pop(el.get("NAME"))
                    rec.update(name=name, parent=parent, openingbalance=el.findtext("OPENINGBALANCE") or "0.00",
                               alter_id=company._next_alter(), **extra)
                    company.ledgers[name] = rec
                    altered += 1
                elif name in company.ledgers:
                    errors.append(f"Ledger '{name}' already exists!")
                else:
                    company.add_ledger(name, parent, el.findtext("OPENINGBALANCE") or "0.00", **extra)
                    created += 1
                continue

            if action in ("Cancel", "Delete", "Alter"):
                target = self._locate(company, el)
                if target is None:
                    errors.append("Voucher does not exist!")
                    continue
                mid = int(target.findtext("MASTERID"))
                if action == "Delete":
                    del company.vouchers[mid]
                    deleted += 1
                    continue
                if action == "Cancel":
                    for child in list(target):
                        if child.tag.endswith(".LIST"):
                            target.remove(child)
                    target.find("ISCANCELLED").text = "Yes"
                    if el.findtext("NARRATION"):
                        target.find("NARRATION").text = el.findtext("NARRATION")
                    target.find("ALTERID").text = f" {company._next_alter()}"
                    cancelled += 1
                    continue
                problems = self._validate(company, el)
                if problems:
                    errors.extend(problems)
                    continue
                keep = {t: target.findtext(t) for t in ("GUID", "MASTERID", "REMOTEID")}
                for child in list(target):
                    target.remove(child)
                for child in el:
                    target.append(copy.deepcopy(child))
                for t, val in keep.items():
                    etree.SubElement(target, t).text = val
                etree.SubElement(target, "ALTERID").text = f" {company._next_alter()}"
                if target.find("ISCANCELLED") is None:
                    etree.SubElement(target, "ISCANCELLED").text = "No"
                altered += 1
                continue

            problems = self._validate(company, el)
            if problems:
                errors.extend(problems)
                continue
            last = self.create_voucher(company, el)
            created += 1
        return self._result(created, altered, deleted, cancelled, last, errors)

    def create_voucher(self, company: MockCompany, el) -> int:
        company.master_id += 1
        mid = company.master_id
        stored = etree.Element("VOUCHER", REMOTEID=el.get("REMOTEID") or "", VCHTYPE=el.get("VCHTYPE") or "",
                               ACTION="Create", OBJVIEW=el.get("OBJVIEW") or "")
        for child in el:
            stored.append(copy.deepcopy(child))
        etree.SubElement(stored, "GUID").text = f"{company.guid}-{mid:08x}"
        etree.SubElement(stored, "MASTERID").text = f" {mid}"
        etree.SubElement(stored, "ALTERID").text = f" {company._next_alter()}"
        etree.SubElement(stored, "REMOTEID").text = el.get("REMOTEID") or ""
        if stored.find("ISCANCELLED") is None:
            etree.SubElement(stored, "ISCANCELLED").text = "No"
        if stored.find("VOUCHERNUMBER") is None:
            etree.SubElement(stored, "VOUCHERNUMBER").text = str(mid)
        company.vouchers[mid] = stored
        return mid

    def enter_voucher(self, company: MockCompany, xml: str) -> int:
        """Simulate a user entering a voucher directly in Tally."""
        with self.lock:
            el = etree.fromstring(xml.encode("utf-8"))
            errors = self._validate(company, el)
            if errors:
                raise ValueError(errors)
            return self.create_voucher(company, el)


def demo(mock: MockTally, name: str) -> MockCompany:
    c = mock.add_company(name)
    c.add_ledger("HDFC Bank", "Bank Accounts", "-250000.00")
    c.add_ledger("Capital", "Capital Account", "250000.00")
    c.add_ledger("Acme Traders", "Sundry Debtors", isbillwiseon=True)
    c.add_ledger("Bharat Supplies", "Sundry Creditors", isbillwiseon=True)
    c.add_ledger("Sales", "Sales Accounts")
    c.add_ledger("Purchases", "Purchase Accounts")
    c.add_ledger("Rent", "Indirect Expenses")
    c.add_ledger("Office Expenses", "Indirect Expenses")
    mock.enter_voucher(c, """<VOUCHER VCHTYPE="Payment"><DATE>20260405</DATE><VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
      <VOUCHERNUMBER>1</VOUCHERNUMBER><NARRATION>April rent</NARRATION>
      <ALLLEDGERENTRIES.LIST><LEDGERNAME>Rent</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>-25000.00</AMOUNT></ALLLEDGERENTRIES.LIST>
      <ALLLEDGERENTRIES.LIST><LEDGERNAME>HDFC Bank</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>25000.00</AMOUNT></ALLLEDGERENTRIES.LIST>
      </VOUCHER>""")
    return c


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock TallyPrime XML server")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--company", default="Demo Trading Co")
    args = parser.parse_args()
    mock = MockTally()
    demo(mock, args.company)
    port = mock.start(args.port, host="127.0.0.1")
    print(f"Mock Tally listening on http://127.0.0.1:{port} with company '{args.company}' (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        mock.stop()


if __name__ == "__main__":
    main()
