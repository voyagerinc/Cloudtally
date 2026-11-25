"""One-click fixes for inbound vouchers that wait for a master ("Stock item 'X' does not exist").

fix_from_tally: ask Tally for that one master by name and bring it in; if Tally has no master of that name,
read the waiting vouchers again from Tally (a master renamed in Tally leaves the old name in the copy kept
here); then try them again.

use_instead: the master is not in Tally under that name any more (renamed or merged there): the waiting
vouchers are saved with a master that is here (the operator picks it, usually from the suggestions)."""
import difflib
import logging
import re
import threading
import uuid
from datetime import datetime
from xml.sax.saxutils import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import (Company, Godown, Ledger, StockItem, SyncInbox, TallyConnector, TallyMapping,
                        VoucherType)
from app.services.canonical import content_hash
from app.services.sync import common, inbound
from app.services.sync.masters_in import MasterImporter

log = logging.getLogger("sync.fix_master")

# kind in the message -> (Tally type, importer method, model, payload keys that hold that kind of name)
KINDS = {
    "Ledger": ("Ledger", "ledgers", Ledger, {"ledger", "party_ledger"}),
    "Stock item": ("Stock Item", "stock_items", StockItem, {"item"}),
    "Godown": ("Godown", "godowns", Godown, {"godown"}),
    "Voucher type": ("Voucher Type", "voucher_types", VoucherType, {"voucher_type"}),
}
_PROBLEM = re.compile(r"^(Ledger|Stock item|Godown|Voucher type) '(.+)' does not exist$", re.S)


def parse_problem(text: str | None) -> tuple[str, str] | None:
    m = _PROBLEM.match((text or "").strip())
    return (m.group(1), m.group(2)) if m else None


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", s.casefold())


def suggestions(db: Session, company_id: uuid.UUID, kind: str, name: str, limit: int = 3) -> list[str]:
    """Masters here whose name is closest to the missing one (same letters and digits first)."""
    model = KINDS[kind][2]
    names = list(db.scalars(select(model.name).where(model.company_id == company_id)))
    exact = [n for n in names if _norm(n) == _norm(name) and n != name]
    by_norm = {}
    for n in names:
        by_norm.setdefault(_norm(n), n)
    close = [by_norm[k] for k in difflib.get_close_matches(_norm(name), list(by_norm), n=limit + 2, cutoff=0.8)]
    out = []
    for n in exact + close:
        if n not in out and n != name:
            out.append(n)
    return out[:limit]


def _waiting(db: Session, company_id: uuid.UUID, ids) -> list[SyncInbox]:
    return list(db.scalars(select(SyncInbox).where(SyncInbox.company_id == company_id, SyncInbox.id.in_(list(ids)),
                                                   SyncInbox.status == "failed")))


def _saved(db: Session, company_id: uuid.UUID, items: list[SyncInbox]) -> tuple[int, int]:
    """(saved, still waiting) of these vouchers, also when a newer copy read from Tally was the one saved."""
    mids = {i.tally_master_id: i.tally_alter_id or 0 for i in items if i.tally_master_id}
    have = dict(db.execute(select(TallyMapping.tally_master_id, TallyMapping.tally_alter_id).where(
        TallyMapping.company_id == company_id, TallyMapping.entity_type == "voucher",
        TallyMapping.tally_master_id.in_(list(mids)))).all()) if mids else {}
    saved = sum(1 for m, a in mids.items() if m in have and (have[m] or 0) >= a)
    return saved, len(mids) - saved


# ------------------------------------------------------------------ fix from Tally (background job)
JOBS: dict[str, dict] = {}
_lock = threading.Lock()


def job(company_id: uuid.UUID) -> dict:
    return JOBS.get(str(company_id)) or {"state": "never"}


def start(company_id: uuid.UUID, problem: str) -> None:
    with _lock:
        if job(company_id).get("state") == "running":
            raise RuntimeError("A fix is already running for this company. Wait until it has finished.")
        JOBS[str(company_id)] = {"state": "running", "problem": problem, "step": "Starting",
                                 "started_at": datetime.now().astimezone().isoformat()}


def _step(company_id, **kw) -> None:
    JOBS[str(company_id)].update(kw)


def fix_from_tally(session_factory, company_id: uuid.UUID, ids: list[uuid.UUID], who: str) -> None:
    try:
        with session_factory() as db:
            _fix(db, company_id, ids, who)
    except Exception as exc:     # shown in the dialog; the vouchers simply stay waiting
        log.exception("fixing a missing master failed for %s", company_id)
        _step(company_id, state="failed", message=f"Could not finish: {exc}")


def _fix(db: Session, company_id: uuid.UUID, ids: list[uuid.UUID], who: str) -> None:
    company, conn = db.get(Company, company_id), db.get(TallyConnector, company_id)
    items = _waiting(db, company_id, ids)
    if not items:
        _step(company_id, state="ok", message="Nothing is waiting any more.", saved=0, still=0)
        return
    parsed = parse_problem(items[0].error)
    if parsed is None or company is None or conn is None:
        raise ValueError("these vouchers do not wait for a master")
    kind, name = parsed
    ctype, method, _, _ = KINDS[kind]
    fetch = next(f for t, f, _ in MasterImporter.SPECS if t == ctype)
    found = False
    client = common.client_for(conn)
    try:
        _step(company_id, step=f"Asking Tally for the {kind.lower()} '{name}'")
        records = client.masters(ctype, fetch, name=name)
        if records:
            found = True
            importer = MasterImporter(db, company, conn)
            getattr(importer, method)(records)
            db.commit()
        _step(company_id, step=f"Trying the {len(items)} voucher(s) again")
        for item in items:
            inbound.apply_item(db, company, conn, item)
            db.commit()
        still = [i for i in items if i.status == "failed" and i.error_code == "missing_master"]
        if still and not found:
            _step(company_id, step=f"'{name}' is not in Tally under this name: reading the {len(still)} voucher(s) "
                                   "again from Tally (a renamed master)")
            inbound.read_again(db, company, conn, client, still)
    finally:
        client.close()
    saved, left = _saved(db, company_id, items)
    if not left:
        msg = f"Fixed: all {saved} voucher(s) saved" + (f" ('{name}' was read from Tally)." if found else ".")
    elif found:
        msg = f"'{name}' was read from Tally; {saved} voucher(s) saved, {left} still waiting for another master."
    else:
        msg = (f"'{name}' is not in Tally under this name (renamed, merged or deleted there). {saved} saved, "
               f"{left} still waiting: choose the {kind.lower()} to use instead, or correct the vouchers in Tally.")
    audit.record(db, audit.Actor(who), "sync.inbox.fix_master", "company", company_id, company_id=company_id,
                 after={"master": f"{kind}: {name}", "found_in_tally": found, "saved": saved, "still": left})
    db.commit()
    _step(company_id, state="ok", step="Done", message=msg, saved=saved, still=left, found_in_tally=found,
          suggestions=suggestions(db, company_id, kind, name) if left and not found else [])


# ------------------------------------------------------------------ use another master here
def _rename(value, keys: set, old: str, new: str):
    if isinstance(value, dict):
        return {k: (new if k in keys and v == old else _rename(v, keys, old, new)) for k, v in value.items()}
    if isinstance(value, list):
        return [_rename(v, keys, old, new) for v in value]
    return value


def use_instead(db: Session, company_id: uuid.UUID, ids: list[uuid.UUID], use: str, actor) -> dict:
    items = _waiting(db, company_id, ids)
    if not items:
        return {"saved": 0, "still": 0}
    parsed = parse_problem(items[0].error)
    if parsed is None or any(parse_problem(i.error) != parsed for i in items):
        raise ValueError("Choose vouchers that wait for the same master.")
    kind, name = parsed
    _, _, model, keys = KINDS[kind]
    if not db.scalar(select(model.id).where(model.company_id == company_id, model.name == use)):
        raise ValueError(f"There is no {kind.lower()} '{use}' here.")
    company, conn = db.get(Company, company_id), db.get(TallyConnector, company_id)
    old_x, new_x = f">{escape(name)}<", f">{escape(use)}<"
    for item in items:
        doc = _rename(item.payload or {}, keys, name, use)
        item.payload = doc
        item.payload_hash = content_hash({k: v for k, v in doc.items() if k not in ("remote_id", "is_deleted")})
        if item.raw_xml:     # "Try again" reads the voucher from this copy of Tally's text
            item.raw_xml = item.raw_xml.replace(old_x, new_x)
        inbound.apply_item(db, company, conn, item)
        db.commit()
    saved, left = _saved(db, company_id, items)
    audit.record(db, actor, "sync.inbox.use_master", "company", company_id, company_id=company_id,
                 before={"master": f"{kind}: {name}"}, after={"master": f"{kind}: {use}", "saved": saved, "still": left},
                 reason="master missing in Tally; another master used for the waiting vouchers")
    db.commit()
    return {"saved": saved, "still": left, "kind": kind, "name": name, "use": use}
