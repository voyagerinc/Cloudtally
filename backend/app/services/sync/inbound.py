"""Tally -> ERP: incremental polling by AlterID, durable inbox, idempotent apply (PRD §6.4)."""
import time
import logging
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import audit
from app.audit import TALLY
from app.connectors.tally import TallyClient, TallyError
from app.connectors.tally import codec
from app.connectors.tally import xmlutil as X
from app.core.config import settings
from app.models import (
    Company, OutboxStatus, SyncInbox, SyncOutbox, SyncStatus, TallyConnector, TallyMapping, Voucher, VoucherStatus,
    VoucherType, VoucherVersion,
)
from app.models.base import utcnow
from app.services.accounting import vouchers as engine
from app.services.canonical import content_hash
from app.services.sync import common
from app.services.sync.masters_in import MasterImporter
from app.services.sync.progress import NoProgress

log = logging.getLogger("sync.inbound")
S = VoucherStatus

MAX_BATCH_TIMEOUT = 600     # seconds: Tally on a very large company, busy with people working in it
SCAN_TIMEOUT = 300          # seconds for the change scan (Tally goes through every voucher once)
SLOW_SCAN_SECONDS = 8       # a change scan this slow means a large company: Tally walks every voucher per request
LARGE_BATCH = 200           # vouchers per request to start with then (measured live, Nov 2026: 500 took 46 s,
LARGE_BATCH_MAX = 500       # 50 took ~35 s); more while Tally's replies stay small (settings.sync_tally_reply_bytes)


def batch_size_for(scan_secs: float) -> int:
    """Vouchers per Tally request. Tally walks every voucher of the company for each request, whatever it
    asks for, so on a large company the walk, not the vouchers, is the cost: ask for more per request."""
    size = settings.sync_voucher_batch_size
    return max(size, LARGE_BATCH) if scan_secs >= SLOW_SCAN_SECONDS else size


def voucher_from(company: Company, conn: TallyConnector) -> date:
    """Tally vouchers dated before this are ignored (the inbound start date, else books-from)."""
    return conn.inbound_from_date or company.books_from


def poll(db: Session, company: Company, conn: TallyConnector, client: TallyClient, *, force: bool = False,
         progress=None) -> dict:
    """One incremental pass: masters then vouchers. Safe to repeat (overlap + idempotent inbox).

    Kept cheap for Tally: the company's change counters are read first (milliseconds) and nothing else is
    asked for when they have not moved. A voucher change scan costs Tally ~15 s on a large company, so it
    runs at most every `sync_voucher_scan_seconds` unless forced (Sync now).
    """
    progress = progress or NoProgress()
    stats = {"masters": {}, "received": 0, "applied": 0, "ignored": 0, "failed": 0, "conflict": 0}
    if not conn.enabled or not conn.inbound_enabled or conn.freeze_inbound:
        stats["skipped"] = "inbound disabled or frozen"
        progress.step(1, "Receiving from Tally is switched off (Connector tab)", state="skipped")
        progress.step(2, "Receiving from Tally is switched off (Connector tab)", state="skipped")
        return stats
    progress.step(1, "Checking Tally's change counters")
    counters = client.company_counters()
    # Tally's counters only grow. Lower than what was read means Tally's data was rebuilt (company split,
    # restored from a backup, rewritten): what was read is no guide to what changed, so read from the start.
    # Masters and vouchers already here are matched and skipped, nothing is duplicated.
    if counters["alt_mst_id"] is not None and counters["alt_mst_id"] < conn.master_watermark:
        log.warning("%s: Tally's master counter %s is below %s; reading every master again",
                    company.name, counters["alt_mst_id"], conn.master_watermark)
        conn.master_watermark = 0
    if counters["alt_vch_id"] is not None and counters["alt_vch_id"] < conn.voucher_watermark:
        log.warning("%s: Tally's voucher counter %s is below %s; scanning every voucher again",
                    company.name, counters["alt_vch_id"], conn.voucher_watermark)
        conn.voucher_watermark = 0

    if counters["alt_mst_id"] is None or counters["alt_mst_id"] > conn.master_watermark:
        importer = MasterImporter(db, company, conn)
        if not conn.master_watermark:      # every master: a big request on a large company
            client.timeout = max(client.timeout, SCAN_TIMEOUT)
        importer.pull(client, since_alter_id=max(0, conn.master_watermark - settings.sync_inbound_overlap) or None,
                      on_type=lambda i, n, label: progress.step(1, f"Reading {label}", done=i, total=n))
        stats["masters"] = importer.counts
        conn.master_watermark = max(conn.master_watermark, importer.max_alter_id)
        db.commit()
        changed = sum(c["created"] + c["updated"] for c in importer.counts.values())
        progress.step(1, f"{changed} master(s) created or updated", state="done")
        if any(c["created"] + c["updated"] for c in importer.counts.values()):
            stats["retried"] = retry_waiting_for_masters(db, company, conn, progress, client)
    else:
        stats["masters"] = "unchanged"
        progress.step(1, "No master changed in Tally", state="done")
    _read_opening_bills(db, company, client, stats, progress)

    if counters["alt_vch_id"] is not None and counters["alt_vch_id"] <= conn.voucher_watermark:
        stats["vouchers"] = "unchanged"
        progress.step(2, "No voucher changed in Tally", state="done")
    elif not force and not conn.voucher_backlog and conn.voucher_scan_at and \
            (wait := settings.sync_voucher_scan_seconds - (utcnow() - conn.voucher_scan_at).total_seconds()) > 0:
        stats["vouchers"] = f"changed; next scan in {int(wait)} s"
        progress.step(2, f"Vouchers changed in Tally; next scan in {int(wait) // 60 + 1} min (a Pull scans at once)",
                      state="skipped")
    else:
        _pull_vouchers(db, company, conn, client, counters["alt_vch_id"], stats, progress)
    if force and _deleted_check_due(conn):
        # Sync now: also find vouchers deleted in Tally. The full list makes Tally go through and list every
        # voucher, so not while a backlog is read, and at most every few hours.
        _remove_deleted_in_tally(db, company, conn, client, stats, progress)
    conn.last_inbound_at = utcnow()
    db.commit()
    return stats


def retry_waiting_for_masters(db: Session, company: Company, conn: TallyConnector, progress=None,
                              client: TallyClient | None = None) -> dict:
    """Vouchers that failed because a ledger or stock item was not here yet: try them again now that new
    masters came in. Only the newest copy of each voucher, and only when nothing newer is already applied."""
    from app.models import TallyMapping
    failed = db.scalars(select(SyncInbox).where(
        SyncInbox.company_id == company.id, SyncInbox.entity_type == "voucher", SyncInbox.status == "failed",
        SyncInbox.error_code == "missing_master").order_by(SyncInbox.tally_master_id, SyncInbox.tally_alter_id.desc())).all()
    newest = {}
    for item in failed:
        newest.setdefault(item.tally_master_id, item)
    if not newest:
        return {}
    have = dict(db.execute(select(TallyMapping.tally_master_id, TallyMapping.tally_alter_id).where(
        TallyMapping.company_id == company.id, TallyMapping.entity_type == "voucher",
        TallyMapping.tally_master_id.in_(list(newest)))).all())
    todo = [i for m, i in newest.items() if m not in have or (have[m] or 0) < (i.tally_alter_id or 0)]
    outcomes: dict = {}
    for n, item in enumerate(todo, 1):
        if progress and n % 100 == 1:
            progress.step(1, "Trying again vouchers that waited for these masters", done=n - 1, total=len(todo))
        outcome = apply_item(db, company, conn, item)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        db.commit()
    if progress:
        progress.step(1, f"{outcomes.get('applied', 0)} of {len(todo)} waiting voucher(s) saved now that their "
                         f"masters are here", state="done")
    still = [i for i in todo if i.status == "failed" and i.error_code == "missing_master"]
    if client is not None and still:
        outcomes["read_again"] = read_again(db, company, conn, client, still, progress)
    return outcomes


def read_again(db: Session, company: Company, conn: TallyConnector, client: TallyClient, items: list,
               progress=None) -> dict:
    """Read these vouchers from Tally afresh and save them. For vouchers still waiting for a master that is
    here under another name: a ledger or stock item renamed in Tally changes the vouchers that use it, but
    not their AlterID, so the copy kept from before still has the old name."""
    by_mid = {i.tally_master_id: i for i in items if i.tally_master_id}
    plan = batch_plan(conn, SLOW_SCAN_SECONDS if len(by_mid) > 200 else 0)
    client.timeout = max(client.timeout, SCAN_TIMEOUT)
    outcomes: dict = {}
    done = 0
    for batch in client.vouchers_in_batches(list(by_mid), **plan):
        done += len(batch)
        if progress:
            progress.step(1, "Reading vouchers that wait for a renamed master from Tally again",
                          done=done, total=len(by_mid))
        for tv in batch:
            item = receive(db, company.id, tv) or by_mid.get(tv.master_id)
            if item is None:
                continue
            if item.tally_alter_id == tv.alter_id:      # same version: take Tally's current text of it
                item.raw_xml = tv.raw_xml
                item.payload = dict(tv.doc, remote_id=tv.remote_id, is_deleted=tv.is_deleted)
                item.payload_hash = content_hash(tv.doc)
            outcome = apply_item(db, company, conn, item)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            db.commit()
    if progress:
        progress.step(1, f"{outcomes.get('applied', 0)} of {len(by_mid)} voucher(s) read again from Tally and saved",
                      state="done")
    return outcomes


def _read_opening_bills(db: Session, company: Company, client: TallyClient, stats: dict, progress) -> None:
    """Once: the opening bill-wise details of ledgers synced before they were read (ageing needs them).
    One light request (name and opening bills only); later changes come with the ledgers themselves."""
    from app.models import Ledger
    missing = db.scalar(select(Ledger.id).where(Ledger.company_id == company.id, Ledger.opening_bills.is_(None),
                                                Ledger.opening_balance != 0).limit(1))
    if missing is None:
        return
    progress.step(1, "Reading opening bill-wise balances from Tally (once)")
    try:
        els = list(client._collection("Ledger", "LEDGER", ["NAME", "BILLALLOCATIONS"]))
    except TallyError as exc:
        log.warning("opening bills not read for %s: %s", company.name, exc.message)
        return
    found = {X.clean_name(el.get("NAME") or X.txt(el, "NAME")): codec.opening_bills(el) for el in els}
    n = 0
    for led in db.scalars(select(Ledger).where(Ledger.company_id == company.id, Ledger.opening_bills.is_(None))):
        led.opening_bills = found.get(led.name, [])
        n += bool(led.opening_bills)
    db.commit()
    stats["opening_bills"] = n
    progress.step(1, f"Opening bill-wise balances read for {n} ledger(s)", state="done")


def _pull_vouchers(db: Session, company: Company, conn: TallyConnector, client: TallyClient,
                   counter: int | None, stats: dict, progress) -> None:
    conn.voucher_scan_at = utcnow()
    db.commit()
    since = max(0, conn.voucher_watermark - settings.sync_inbound_overlap)
    start = voucher_from(company, conn)
    progress.step(2, "Asking Tally which vouchers changed (this takes about 15 s on a large company)")
    scan_started = time.monotonic()
    client.timeout = max(client.timeout, SCAN_TIMEOUT)     # a busy Tally on a large company needs minutes
    index = client.voucher_index(f"$AlterID > {since}")
    # Tally walks every voucher for each request, so a batch takes at least as long as this scan, and longer
    # while people work in Tally. Give it the time a company of this size needs instead of failing at 60 s.
    scan_secs = time.monotonic() - scan_started
    client.timeout = max(client.timeout, min(MAX_BATCH_TIMEOUT, 3 * scan_secs + 60))
    plan = batch_plan(conn, scan_secs)
    size = plan["batch_size"]
    # Oldest changes first (by AlterID), so everything below the first change left over has been read
    # and the watermark can move up after every pass; each later scan of a backlog is then smaller.
    wanted = sorted(_unseen(db, company.id, [(r.master_id, r.alter_id) for r in index if not r.date or r.date >= start]),
                    key=lambda c: c[1])
    limit = settings.sync_voucher_max_batches * size
    wanted, rest = wanted[:limit], wanted[limit:]
    stats["scanned"], stats["to_fetch"], stats["remaining"] = len(index), len(wanted) + len(rest), len(rest)
    frozen = set(conn.frozen_voucher_types or [])
    high = rest[0][1] - 1 if rest else max([conn.voucher_watermark, counter or 0, *(r.alter_id for r in index)])
    held = None
    done, total = 0, len(wanted)
    # What is still to read, kept up to date batch by batch: if Tally stops answering part-way, Admin > Pull / Push
    # shows the vouchers left instead of "all read", and the next sync carries on with them.
    conn.voucher_backlog = total + len(rest)
    db.commit()
    progress.vouchers_left(total + len(rest))
    progress.step(2, f"{total} changed voucher(s) to fetch" + (f" ({stats['remaining']} more on later syncs)" if stats["remaining"] else ""),
                  done=0, total=total)
    for n, batch in enumerate(client.vouchers_in_batches([m for m, _ in wanted], extra=f"$AlterID > {since}",
                                                         **plan), 1):
        done += len(batch)
        progress.vouchers_read(len(batch))
        conn.voucher_backlog = max(0, total - done) + len(rest)
        progress.step(2, f"Fetched batch {n} from Tally; saving", done=done, total=total)
        for tv in sorted(batch, key=lambda t: t.alter_id):
            if tv.doc.get("voucher_type") in frozen:
                held = tv.alter_id if held is None else min(held, tv.alter_id)
                continue    # picked up after unfreezing (watermark is held below it)
            item = receive(db, company.id, tv)
            if item is not None:
                stats["received"] += 1
                outcome = apply_item(db, company, conn, item)
                stats[outcome] = stats.get(outcome, 0) + 1
            db.commit()
    # A pass interrupted part-way is simply repeated: the inbox and _unseen skip what already arrived.
    conn.voucher_watermark = max(conn.voucher_watermark, high if held is None else min(high, held - 1))
    conn.voucher_backlog = len(rest)
    progress.step(2, f"{stats['received']} voucher change(s) saved"
                  + (f"; {stats['remaining']} more on the next sync" if stats["remaining"] else ""),
                  done=total, total=total, state="done")


def _deleted_check_due(conn: TallyConnector) -> bool:
    if conn.voucher_backlog:
        return False
    at = conn.deleted_check_at
    return at is None or (utcnow() - at).total_seconds() >= settings.sync_deleted_check_hours * 3600


def batch_plan(conn: TallyConnector, scan_secs: float) -> dict:
    """How vouchers are asked from Tally: a size chosen on the connector is kept (only halved when Tally
    cannot answer it in time); on Automatic it follows the size of Tally's replies."""
    if conn.voucher_batch_size:
        return {"batch_size": conn.voucher_batch_size}
    size = batch_size_for(scan_secs)
    return {"batch_size": size, "max_size": LARGE_BATCH_MAX if size >= LARGE_BATCH else size,
            "max_bytes": settings.sync_tally_reply_bytes}


def _remove_deleted_in_tally(db: Session, company: Company, conn: TallyConnector, client: TallyClient,
                             stats: dict, progress) -> None:
    """Tally does not report deletions: a deleted voucher just stops being listed. Every synced voucher
    missing from Tally's list (and not found when looked up on its own) is deleted in the software too.
    A software change still waiting to go to Tally is left to the outbound step, which raises a conflict.
    If very many go missing at once (a wrong or restored company, say) nothing is deleted."""
    mapped = db.execute(
        select(TallyMapping.erp_id, TallyMapping.tally_master_id).join(Voucher, Voucher.id == TallyMapping.erp_id)
        .where(TallyMapping.company_id == company.id, TallyMapping.entity_type == "voucher",
               TallyMapping.tally_master_id.is_not(None), Voucher.status != S.DELETED,
               Voucher.voucher_date >= voucher_from(company, conn))).all()
    if not mapped:
        return
    progress.step(2, "Checking for vouchers deleted in Tally")
    alive = {r.master_id for r in client.voucher_index(None)}
    conn.deleted_check_at = utcnow()
    db.commit()
    gone = [(erp_id, mid) for erp_id, mid in mapped if mid not in alive]
    if not gone:
        return
    if not alive or len(gone) > max(25, len(mapped) // 50):
        stats["deleted_in_tally_held"] = len(gone)
        log.warning("%s: %d synced vouchers missing in Tally; not deleting them automatically", company.name, len(gone))
        progress.step(2, f"{len(gone)} vouchers are missing in Tally - too many to delete automatically; "
                         "check the company in Tally and the reconciliation", state="done")
        return
    pending = set(db.scalars(select(SyncOutbox.entity_id).where(
        SyncOutbox.entity_type == "voucher", SyncOutbox.entity_id.in_([e for e, _ in gone]),
        SyncOutbox.status.in_([OutboxStatus.PENDING, OutboxStatus.FAILED]))))
    removed = 0
    for erp_id, mid in gone:
        if erp_id in pending or client.vouchers(master_id=mid):
            continue
        v = db.get(Voucher, erp_id)
        engine.transition(db, v, "delete", TALLY, reason="Deleted in Tally", source="tally")
        mapping = common.mapping_for_erp(db, company.id, "voucher", v.id)
        mapping.last_synced_hash, mapping.erp_version_synced = "deleted", v.version
        v.sync_status = SyncStatus.SYNCED
        removed += 1
        db.commit()
    stats["deleted_in_tally"] = removed
    if removed:
        progress.step(2, f"{removed} voucher(s) deleted in Tally removed from the software", state="done")


def _unseen(db: Session, company_id: uuid.UUID, changes: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Drop (MasterID, AlterID) pairs already received or already written by us (the overlap re-reads them)."""
    if not changes:
        return []
    mids = sorted({m for m, _ in changes})
    known: set = set()
    # In chunks: a large company's first sync lists more vouchers than one query may carry (65,535 values).
    for i in range(0, len(mids), CHUNK):
        part = mids[i:i + CHUNK]
        known |= set(db.execute(select(SyncInbox.tally_master_id, SyncInbox.tally_alter_id).where(
            SyncInbox.company_id == company_id, SyncInbox.entity_type == "voucher",
            SyncInbox.tally_master_id.in_(part))))
        known |= set(db.execute(select(TallyMapping.tally_master_id, TallyMapping.tally_alter_id).where(
            TallyMapping.company_id == company_id, TallyMapping.entity_type == "voucher",
            TallyMapping.tally_master_id.in_(part))))
    return [c for c in changes if c not in known]


CHUNK = 10000


def receive(db: Session, company_id: uuid.UUID, tv: codec.TallyVoucher) -> SyncInbox | None:
    """Store the change once; a repeated delivery of the same (guid, alter_id) is a no-op."""
    payload = dict(tv.doc, remote_id=tv.remote_id, is_deleted=tv.is_deleted)
    stmt = insert(SyncInbox).values(
        id=uuid.uuid4(), company_id=company_id, entity_type="voucher", tally_guid=tv.guid,
        tally_master_id=tv.master_id, tally_alter_id=tv.alter_id, voucher_type=tv.doc.get("voucher_type"),
        voucher_number=tv.doc.get("voucher_number"), payload=payload, raw_xml=tv.raw_xml,
        payload_hash=content_hash(tv.doc), status="received", attempts=0, received_at=utcnow(),
    ).on_conflict_do_nothing(index_elements=["company_id", "entity_type", "tally_guid", "tally_alter_id"]).returning(SyncInbox.id)
    new_id = db.execute(stmt).scalar()
    db.flush()
    return db.get(SyncInbox, new_id) if new_id else None


def _finish(item: SyncInbox, status: str, *, erp_id=None, code=None, error=None) -> str:
    item.status, item.erp_id = status, erp_id or item.erp_id
    item.error_code, item.error, item.processed_at = code, error, utcnow()
    return status


def apply_item(db: Session, company: Company, conn: TallyConnector, item: SyncInbox) -> str:
    item.attempts += 1
    doc = {k: v for k, v in item.payload.items() if k not in ("remote_id", "is_deleted")}
    incoming_hash = item.payload_hash
    mapping = common.mapping_for_guid(db, company.id, "voucher", item.tally_guid)

    if mapping is None:
        mapping = _match_unmapped(db, company, conn, item, doc)

    try:
        with db.begin_nested():
            if mapping is None:
                return _create_from_tally(db, company, conn, item, doc)
            if mapping.tally_alter_id is not None and item.tally_alter_id < mapping.tally_alter_id:
                return _finish(item, "ignored", erp_id=mapping.erp_id, error="older than last synchronized version")
            v = db.get(Voucher, mapping.erp_id)
            if incoming_hash in (mapping.last_synced_hash, v.content_hash):
                # Our own write coming back, or both sides already identical.
                if incoming_hash == v.content_hash:
                    mapping.erp_version_synced = v.version
                    if v.sync_status != SyncStatus.PENDING:
                        v.sync_status = SyncStatus.SYNCED
                mapping.tally_alter_id, mapping.tally_master_id = item.tally_alter_id, item.tally_master_id
                mapping.last_synced_hash = incoming_hash
                if doc.get("statutory") and doc["statutory"] != (v.statutory or {}):
                    v.statutory = doc["statutory"]   # e-invoice / e-way details: not accounting content
                if "custom_fields" in doc and doc["custom_fields"] != (v.custom_fields or {}):
                    v.custom_fields = doc["custom_fields"]   # custom (TDL) fields: not accounting content either
                return _finish(item, "ignored", erp_id=v.id, error="no content change")
            local_unsynced = v.version > (mapping.erp_version_synced or 0) and v.content_hash != mapping.last_synced_hash
            if local_unsynced:
                base = db.scalar(select(VoucherVersion.snapshot).where(
                    VoucherVersion.voucher_id == v.id, VoucherVersion.version == mapping.erp_version_synced))
                common.open_conflict(db, company.id, v.id, detected_by="inbound", erp_doc=engine.to_doc(v),
                                     tally_doc=doc, tally_guid=item.tally_guid, tally_alter_id=item.tally_alter_id,
                                     base_doc=base)
                v.sync_status = SyncStatus.CONFLICT
                return _finish(item, "conflict", erp_id=v.id, code="conflict",
                               error="Changed in both systems since last sync")
            apply_tally_doc(db, v, doc, actor=TALLY, reason=f"Tally alteration (AlterID {item.tally_alter_id})")
            common.save_mapping(db, conn, "voucher", v.id, guid=item.tally_guid, master_id=item.tally_master_id,
                                alter_id=item.tally_alter_id, content_hash=v.content_hash, erp_version=v.version,
                                direction="inbound", voucher_number=v.voucher_number, voucher_date=v.voucher_date,
                                mapping=mapping)
            v.sync_status = SyncStatus.SYNCED
            return _finish(item, "applied", erp_id=v.id)
    except (engine.VoucherError, TallyError) as exc:
        return _finish(item, "failed", code=exc.code, error=exc.message)
    except Exception as exc:
        log.exception("inbox item %s failed", item.id)
        return _finish(item, "failed", code="validation_failure", error=f"{type(exc).__name__}: {exc}")


def apply_tally_doc(db: Session, v: Voucher, doc: dict, *, actor, reason: str) -> None:
    """Make the ERP voucher match a Tally document (alteration, cancellation, or reinstatement)."""
    if doc.get("is_cancelled"):
        if v.status != S.CANCELLED:
            engine.transition(db, v, "cancel", actor, reason=reason, source="tally")
        return
    if v.status == S.CANCELLED:
        raise engine.VoucherError("validation_failure", "Voucher is cancelled in ERP but active in Tally")
    if v.status != S.POSTED:
        v.status = S.POSTED
        v.posted_at = v.posted_at or utcnow()
    engine.update_voucher(db, v, doc, actor, expected_version=None, source="tally", reason=reason)


def _match_unmapped(db: Session, company: Company, conn: TallyConnector, item: SyncInbox, doc: dict) -> TallyMapping | None:
    """Fallback identity: our own REMOTEID, else an exact single content match. Never guess (PRD §6.3)."""
    remote = item.payload.get("remote_id")
    candidate = None
    try:
        rid = uuid.UUID(remote) if remote else None
    except ValueError:
        rid = None
    if rid:
        candidate = db.get(Voucher, rid)
        if candidate is not None and candidate.company_id != company.id:
            candidate = None
    if candidate is None and doc.get("voucher_number"):
        vt = db.scalar(select(VoucherType).where(VoucherType.company_id == company.id,
                                                 VoucherType.name == doc.get("voucher_type")))
        if vt is not None:
            rows = db.scalars(select(Voucher).where(
                Voucher.company_id == company.id, Voucher.voucher_type_id == vt.id,
                Voucher.voucher_number == doc["voucher_number"], Voucher.voucher_date == date.fromisoformat(doc["date"]),
                Voucher.status != S.DELETED)).all()
            rows = [r for r in rows if r.content_hash == item.payload_hash
                    and common.mapping_for_erp(db, company.id, "voucher", r.id) is None]
            if len(rows) == 1:
                candidate = rows[0]
    if candidate is None or common.mapping_for_erp(db, company.id, "voucher", candidate.id):
        return None
    return common.save_mapping(db, conn, "voucher", candidate.id, guid=item.tally_guid, master_id=item.tally_master_id,
                               alter_id=None, content_hash=candidate.content_hash, erp_version=candidate.version,
                               direction="outbound", voucher_type=doc.get("voucher_type"),
                               voucher_number=candidate.voucher_number, voucher_date=candidate.voucher_date)


def _create_from_tally(db: Session, company: Company, conn: TallyConnector, item: SyncInbox, doc: dict) -> str:
    if doc.get("is_cancelled") and len(doc.get("entries") or []) < 2:
        return _finish(item, "ignored", error="Cancelled in Tally before it was ever imported")
    status = S.CANCELLED if doc.get("is_cancelled") else S.POSTED
    v = engine.create_voucher(db, company, doc, TALLY, source="tally", initial_status=status,
                              reason=f"Imported from Tally (MasterID {item.tally_master_id})")
    v.sync_status = SyncStatus.SYNCED
    common.save_mapping(db, conn, "voucher", v.id, guid=item.tally_guid, master_id=item.tally_master_id,
                        alter_id=item.tally_alter_id, content_hash=v.content_hash, erp_version=v.version,
                        direction="inbound", voucher_type=doc.get("voucher_type"), voucher_number=v.voucher_number,
                        voucher_date=v.voucher_date)
    audit.record(db, TALLY, "sync.inbound.create", "voucher", v.id, company_id=company.id,
                 after={"tally_guid": item.tally_guid, "alter_id": item.tally_alter_id})
    return _finish(item, "applied", erp_id=v.id)


def reprocess(db: Session, item: SyncInbox) -> str:
    """Operator action: retry an inbox item after fixing masters/periods (or after a parser fix)."""
    company = db.get(Company, item.company_id)
    conn = db.get(TallyConnector, item.company_id)
    if item.raw_xml and item.entity_type == "voucher":
        root = X.parse(item.raw_xml)
        el = root if root.tag == "VOUCHER" else next(root.iter("VOUCHER"), None)
        if el is not None:
            tv = codec.parse_voucher(el)
            item.payload = dict(tv.doc, remote_id=tv.remote_id, is_deleted=tv.is_deleted)
            item.payload_hash = content_hash(tv.doc)
    outcome = apply_item(db, company, conn, item)
    db.commit()
    return outcome
