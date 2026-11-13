"""Tally XML-over-HTTP client (PRD §6.2). Only supported interfaces are used; no file access."""
import threading
import time
from dataclasses import dataclass
from datetime import date

import httpx

from app.connectors.tally import codec
from app.connectors.tally import xmlutil as X
from app.connectors.tally.errors import ErrorCode, TallyError, classify_tally_message
from app.core.config import settings


def _records(root, tag_name: str):
    """Collection records only – Tally's <CMPINFO> summary reuses the same tag names for counts."""
    return (el for el in root.iter(tag_name) if len(el) or el.get("NAME"))


@dataclass
class Exchange:
    request_xml: str
    response_xml: str
    duration_ms: int


class _Gate:
    """One per Tally address: our requests take turns, and after a timeout we wait for Tally to recover."""

    def __init__(self):
        self.lock = threading.Lock()
        self.recovering = False     # a request timed out; Tally may still be working on it


_gates: dict[str, _Gate] = {}
_gates_lock = threading.Lock()


def _gate(url: str) -> _Gate:
    with _gates_lock:
        return _gates.setdefault(url, _Gate())


class TallyClient:
    def __init__(self, host: str, port: int, company: str, *, timeout: float | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.url = f"http://{host}:{port}"
        self.company = company
        self.timeout = timeout or settings.tally_timeout
        self._http = httpx.Client(timeout=httpx.Timeout(self.timeout, connect=5.0), transport=transport)
        self.last: Exchange | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ transport

    def post(self, xml: str) -> str:
        gate = _gate(self.url)
        if not gate.lock.acquire(timeout=settings.tally_queue_seconds):
            raise TallyError(ErrorCode.CONNECTIVITY, "Tally is still busy with an earlier request from Tally Cloud. "
                             "Nothing more was sent; it continues later.", request_xml=xml)
        try:
            self._wait_for_recovery(gate, xml)
            return self._send(xml, gate)
        finally:
            gate.lock.release()

    def _wait_for_recovery(self, gate: _Gate, xml: str) -> None:
        """After a timeout, send nothing until Tally answers again: a request queued behind the one it is
        still working on is what makes Tally hang."""
        if not gate.recovering:
            return
        deadline = time.monotonic() + settings.tally_recovery_seconds
        while True:
            try:
                if self._http.get(self.url, timeout=5.0).status_code == 200:
                    gate.recovering = False
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise TallyError(ErrorCode.CONNECTIVITY, "Tally has not finished an earlier request yet and does "
                                 "not answer. Nothing more was sent; sync continues once Tally is free.",
                                 request_xml=xml)
            time.sleep(5)

    def _send(self, xml: str, gate: _Gate) -> str:
        started = time.monotonic()
        try:
            resp = self._http.post(self.url, content=xml.encode("utf-8"),
                                   headers={"Content-Type": "text/xml;charset=utf-8"})
        except httpx.ConnectError as exc:
            raise TallyError(ErrorCode.CONNECTIVITY, f"Cannot reach Tally at {self.url}: {exc}", request_xml=xml)
        except httpx.TimeoutException as exc:
            if not isinstance(exc, httpx.ConnectTimeout):
                gate.recovering = True      # Tally is probably still working on this request
            # A write may have been applied even though we never saw the answer.
            raise TallyError(ErrorCode.CONNECTIVITY, f"Tally timed out after {self.timeout}s: {exc!r}",
                             outcome_unknown=not isinstance(exc, httpx.ConnectTimeout), request_xml=xml)
        except httpx.HTTPError as exc:
            raise TallyError(ErrorCode.CONNECTIVITY, f"HTTP error talking to Tally: {exc!r}",
                             outcome_unknown=True, request_xml=xml)
        text = X.decode_response(resp.content)
        self.last = Exchange(xml, text, int((time.monotonic() - started) * 1000))
        if resp.status_code != 200:
            raise TallyError(ErrorCode.CONNECTIVITY, f"Tally returned HTTP {resp.status_code}",
                             request_xml=xml, response_xml=text)
        return text

    # ------------------------------------------------------------ reads

    def ping(self) -> str:
        try:
            resp = self._http.get(self.url, timeout=5.0)
        except httpx.ReadTimeout:
            raise TallyError(ErrorCode.CONNECTIVITY, f"Tally at {self.url} accepted the connection but did not respond "
                                                     "within 5 s (it is open but busy or frozen)")
        except httpx.ConnectError:
            raise TallyError(ErrorCode.CONNECTIVITY, f"Nothing is listening at {self.url} (TallyPrime is not running, "
                                                     "or its XML server is off or on another port)")
        except httpx.HTTPError as exc:
            raise TallyError(ErrorCode.CONNECTIVITY, f"Cannot reach Tally at {self.url}: {exc!r}")
        return resp.text.strip()

    def list_companies(self) -> list[dict]:
        xml = codec.export_collection("Company", ["NAME", "GUID", "STARTINGFROM", "BOOKSFROM"], None)
        root = X.parse(self.post(xml))
        return [
            {"name": X.clean_name(c.get("NAME") or X.txt(c, "NAME")), "guid": X.txt(c, "GUID"),
             "starting_from": str(X.parse_date(X.txt(c, "STARTINGFROM")) or ""),
             "books_from": str(X.parse_date(X.txt(c, "BOOKSFROM")) or "")}
            for c in _records(root, "COMPANY")
        ]

    def _collection(self, ctype: str, tag_name: str, fetch: list[str], formula: str | None = None,
                    from_date: date | None = None, to_date: date | None = None):
        xml = codec.export_collection(ctype, fetch, self.company, formula=formula,
                                      from_date=from_date, to_date=to_date)
        text = self.post(xml)
        root = X.parse(text)
        self._check_company(root, text)
        return _records(root, tag_name)

    def _check_company(self, root, text: str) -> None:
        line_errors = [e.text for e in root.iter("LINEERROR") if e.text]
        if line_errors:
            msg = "; ".join(line_errors)
            raise TallyError(classify_tally_message(msg), msg, response_xml=text)

    def masters(self, ctype: str, fetch: list[str], *, since_alter_id: int | None = None,
                name: str | None = None) -> list[dict]:
        formula = f"$AlterID > {int(since_alter_id)}" if since_alter_id else None
        if name is not None:
            formula = '$Name = "' + name.replace('"', "") + '"'
        return [codec.parse_master(el) for el in self._collection(ctype, ctype.upper().replace(" ", ""), fetch, formula)]

    def vouchers(self, *, master_id: int | None = None, remote_id: str | None = None,
                 formula: str | None = None, fetch: list[str] | None = None) -> list[codec.TallyVoucher]:
        """Full vouchers matching one filter. There is deliberately no date range: Tally ignores it."""
        if formula is None and master_id is not None:
            formula = f"$MasterID = {int(master_id)}"
        elif formula is None and remote_id is not None:
            formula = f'$RemoteID = "{remote_id}"'
        if formula is None:
            raise ValueError("refusing to export every voucher with its lines in one request")
        els = self._collection("Voucher", "VOUCHER", fetch or codec.VOUCHER_FETCH, formula)
        return [codec.parse_voucher(el) for el in els]

    # Measured on a 40k-voucher company (Nov 2026): Tally ignores SVFROMDATE/SVTODATE for Voucher
    # collections and walks every voucher for any filter (~15 s), and a full voucher is ~37 KB of XML.
    # So never ask for "all vouchers with lines"; find what changed with a header-only index, then
    # fetch those in small MasterID-range batches.

    def company_counters(self) -> dict:
        """Tally's company-wide last AlterIDs for vouchers and masters. Costs milliseconds."""
        xml = codec.export_collection("Company", ["NAME", "ALTVCHID", "ALTMSTID"], self.company,
                                      formula='$Name = "' + self.company.replace('"', "") + '"')
        el = next(_records(X.parse(self.post(xml)), "COMPANY"), None)

        def num(tag):
            v = X.txt(el, tag) if el is not None else None
            return int(v) if v and v.strip().lstrip("-").isdigit() else None
        return {"alt_vch_id": num("ALTVCHID"), "alt_mst_id": num("ALTMSTID")}

    def voucher_index(self, formula: str | None = None) -> list[codec.VoucherRef]:
        """MasterID, AlterID and date of matching vouchers, without their lines."""
        els = self._collection("Voucher", "VOUCHER", ["MASTERID", "ALTERID", "DATE"], formula)
        return [codec.VoucherRef(int(X.txt(e, "MASTERID") or 0), int(X.txt(e, "ALTERID") or 0),
                                 X.parse_date(X.txt(e, "DATE"))) for e in els]

    def vouchers_in_batches(self, master_ids, *, extra: str | None = None, batch_size: int | None = None,
                            pause: float | None = None):
        """Yield full vouchers for the given MasterIDs, one MasterID range per request."""
        ids = sorted(set(master_ids))
        size = max(1, batch_size or settings.sync_voucher_batch_size)
        pause = settings.sync_voucher_batch_pause if pause is None else pause
        for i in range(0, len(ids), size):
            part = ids[i:i + size]
            if i and pause:
                time.sleep(pause)   # let people working in Tally get a turn
            formula = f"$MasterID >= {part[0]} AND $MasterID <= {part[-1]}" + (f" AND {extra}" if extra else "")
            wanted = set(part)
            yield [v for v in self.vouchers(formula=formula) if v.master_id in wanted]

    # ------------------------------------------------------------ writes

    def import_data(self, report: str, body: str) -> codec.ImportResult:
        xml = codec.import_envelope(report, self.company, body)
        text = self.post(xml)
        result = codec.parse_import_response(text)
        if not result.ok:
            msg = "; ".join(result.line_errors) or f"Tally reported {result.errors} error(s)"
            raise TallyError(classify_tally_message(msg), msg, request_xml=xml, response_xml=text)
        return result
