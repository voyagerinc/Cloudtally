"""ERP -> Tally: claim outbox events, write to Tally idempotently, verify by read-back (PRD §6.4)."""
import logging
import socket
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import audit
from app.audit import TALLY
from app.connectors.tally import ErrorCode, TallyClient, TallyError
from app.connectors.tally import codec
from app.core.config import settings
from app.models import (
    Company, Ledger, OutboxStatus, SyncAttempt, SyncOutbox, SyncStatus, TallyConnector, TallyMapping, Voucher, VoucherVersion,
)
from app.models.base import utcnow
from app.services.accounting.masters import ledger_doc
from app.services.accounting.vouchers import to_doc
from app.services.canonical import content_hash, diff
from app.services.sync import common
from app.services.sync.progress import NoProgress

log = logging.getLogger("sync.outbound")
WORKER_ID = f"{socket.gethostname()}:{uuid.uuid4().hex[:6]}"
STALE_LOCK = timedelta(minutes=10)


class Conflict(Exception):
    pass


def recover_stale(db: Session) -> int:
    """Events left 'processing' by a crashed worker go back to the queue, flagged as outcome-unknown."""
    stale = db.scalars(select(SyncOutbox).where(
        SyncOutbox.status == OutboxStatus.PROCESSING, SyncOutbox.locked_at < utcnow() - STALE_LOCK)).all()
    for ev in stale:
        ev.status, ev.outcome_unknown, ev.locked_at = OutboxStatus.PENDING, True, None
    db.commit()
    return len(stale)


def claim(db: Session, limit: int, company_id: uuid.UUID | None = None) -> list[uuid.UUID]:
    """Lock and mark a batch of due events. SKIP LOCKED lets several workers run safely."""
    conns = {c.company_id: c for c in db.scalars(select(TallyConnector))}
    runnable = [cid for cid, c in conns.items()
                if c.enabled and c.outbound_enabled and not c.freeze_outbound and (company_id in (None, cid))]
    if not runnable:
        return []
    stmt = (select(SyncOutbox)
            .where(SyncOutbox.status == OutboxStatus.PENDING, SyncOutbox.next_attempt_at <= utcnow(),
                   SyncOutbox.company_id.in_(runnable))
            .order_by(SyncOutbox.created_at).limit(limit).with_for_update(skip_locked=True))
    events = []
    for ev in db.scalars(stmt):
        frozen = conns[ev.company_id].frozen_voucher_types or []
        if ev.voucher_type and ev.voucher_type in frozen:
            continue
        ev.status, ev.locked_at = OutboxStatus.PROCESSING, utcnow()
        events.append(ev.id)
    db.commit()
    return events


def process_batch(session_factory: sessionmaker, limit: int | None = None, company_id=None, progress=None) -> dict:
    progress = progress or NoProgress()
    stats = {"claimed": 0, "completed": 0, "retry": 0, "failed": 0, "conflict": 0}
    progress.step(3, "Looking for ERP changes waiting to go to Tally")
    with session_factory() as db:
        recover_stale(db)
        ids = claim(db, limit or settings.sync_batch_size, company_id)
    stats["claimed"] = len(ids)
    # Ledgers first so vouchers in the same batch find their masters.
    with session_factory() as db:
        order = {i: (0 if db.get(SyncOutbox, i).entity_type == "ledger" else 1) for i in ids}
    for n, event_id in enumerate(sorted(ids, key=lambda i: order[i])):
        progress.step(3, f"Sending {n + 1} of {len(ids)} to Tally and checking what Tally stored", done=n, total=len(ids))
        outcome = process_event(session_factory, event_id)
        stats[outcome] = stats.get(outcome, 0) + 1
    progress.step(3, (f"{stats['completed']} of {len(ids)} sent and verified" if ids else "Nothing waiting to send"),
                  done=len(ids), total=len(ids) or None, state="done")
    return stats


def process_event(session_factory: sessionmaker, event_id: uuid.UUID) -> str:
    with session_factory() as db:
        ev = db.get(SyncOutbox, event_id)
        conn = db.get(TallyConnector, ev.company_id)
        ev.attempts += 1
        attempt = SyncAttempt(outbox_id=ev.id, success=False)
        client = common.client_for(conn)
        try:
            if ev.entity_type == "voucher":
                _sync_voucher(db, ev, conn, client)
            elif ev.entity_type == "ledger":
                _sync_ledger(db, ev, conn, client)
            else:
                raise TallyError(ErrorCode.UNSUPPORTED_FIELD, f"Entity type {ev.entity_type} is not synchronized")
            ev.status, ev.completed_at, ev.error_code, ev.last_error = OutboxStatus.COMPLETED, utcnow(), None, None
            ev.outcome_unknown = False
            attempt.success = True
            outcome = "completed"
        except Conflict as exc:
            ev.status, ev.error_code, ev.last_error = OutboxStatus.CONFLICT, ErrorCode.CONFLICT, str(exc)
            outcome = "conflict"
        except TallyError as exc:
            outcome = _handle_error(db, ev, exc)
            attempt.error_code, attempt.error = exc.code, exc.message
            attempt.request_xml, attempt.response_xml = exc.request_xml, exc.response_xml
        except Exception as exc:   # bug or unexpected data: never lose the event
            log.exception("outbox event %s failed", event_id)
            attempts = ev.attempts
            db.rollback()
            ev, conn = db.get(SyncOutbox, event_id), db.get(TallyConnector, ev.company_id)
            ev.attempts = attempts
            attempt = SyncAttempt(outbox_id=ev.id, success=False, error_code=ErrorCode.VALIDATION, error=str(exc))
            outcome = _handle_error(db, ev, TallyError(ErrorCode.VALIDATION, f"{type(exc).__name__}: {exc}"))
        finally:
            client.close()
        if client.last and not attempt.request_xml:
            attempt.request_xml, attempt.response_xml = client.last.request_xml, client.last.response_xml
            attempt.duration_ms = client.last.duration_ms
        ev.locked_at = None
        conn.last_outbound_at = utcnow()
        db.add(attempt)
        audit.record(db, TALLY, f"sync.outbound.{outcome}", ev.entity_type, ev.entity_id, company_id=ev.company_id,
                     after={"event": str(ev.id), "operation": ev.operation, "error": ev.last_error})
        db.commit()
        return outcome


def _handle_error(db: Session, ev: SyncOutbox, exc: TallyError) -> str:
    ev.error_code, ev.last_error = exc.code, exc.message
    ev.outcome_unknown = ev.outcome_unknown or exc.outcome_unknown
    transient = exc.transient
    # A voucher that references a ledger still waiting in the outbox will succeed once that ledger syncs.
    if exc.code == ErrorCode.MISSING_MASTER and ev.entity_type == "voucher":
        transient = transient or db.scalar(select(SyncOutbox.id).where(
            SyncOutbox.company_id == ev.company_id, SyncOutbox.entity_type == "ledger",
            SyncOutbox.status.in_([OutboxStatus.PENDING, OutboxStatus.PROCESSING])).limit(1)) is not None
    if transient and ev.attempts < settings.sync_max_attempts:
        ev.status = OutboxStatus.PENDING
        ev.next_attempt_at = utcnow() + common.backoff_delay(ev.attempts)
        outcome = "retry"
    else:
        ev.status = OutboxStatus.FAILED
        outcome = "failed"
    if ev.entity_type == "voucher":
        v = db.get(Voucher, ev.entity_id)
        if v is not None:
            v.sync_status = SyncStatus.PENDING if outcome == "retry" else SyncStatus.FAILED
    return outcome


# ------------------------------------------------------------------ vouchers

def _ref(conn: TallyConnector, mapping: TallyMapping, erp_id) -> codec.TallyRef:
    return codec.TallyRef(master_id=mapping.tally_master_id, guid=mapping.tally_guid, remote_id=str(erp_id),
                          alter_by=conn.alter_by)


def _read_back(client: TallyClient, v: Voucher, *, master_id=None, remote_id=None) -> codec.TallyVoucher | None:
    found = client.vouchers(master_id=master_id, remote_id=None if master_id else remote_id)
    return found[0] if found else None


def _sync_voucher(db: Session, ev: SyncOutbox, conn: TallyConnector, client: TallyClient) -> None:
    v = db.get(Voucher, ev.entity_id)
    if v is None:
        raise TallyError(ErrorCode.VALIDATION, "Voucher no longer exists in ERP")
    mapping = common.mapping_for_erp(db, v.company_id, "voucher", v.id)
    doc = to_doc(v)

    if mapping is None and (ev.outcome_unknown or ev.attempts > 1):
        # A previous attempt may have created it: look it up by our REMOTEID before creating again.
        existing = _read_back(client, v, remote_id=str(v.id))
        if existing:
            mapping = common.save_mapping(
                db, conn, "voucher", v.id, guid=existing.guid, master_id=existing.master_id,
                alter_id=existing.alter_id, content_hash=content_hash(existing.doc), erp_version=None,
                direction="outbound", voucher_type=doc["voucher_type"], voucher_number=v.voucher_number,
                voucher_date=v.voucher_date)

    if ev.operation in ("cancel", "delete"):
        if mapping is None:
            v.sync_status = SyncStatus.NOT_SYNCED   # never reached Tally: nothing to undo
            return
        action = "Cancel" if ev.operation == "cancel" else "Delete"
        client.import_data("Vouchers", codec.voucher_xml(doc, action=action, erp_id=str(v.id), ref=_ref(conn, mapping, v.id)))
        after = None if action == "Delete" else _read_back(client, v, master_id=mapping.tally_master_id)
        common.save_mapping(db, conn, "voucher", v.id, guid=mapping.tally_guid, master_id=mapping.tally_master_id,
                            alter_id=after.alter_id if after else mapping.tally_alter_id,
                            content_hash=content_hash(after.doc) if after else "deleted",
                            erp_version=v.version, direction="outbound", mapping=mapping)
        v.sync_status = SyncStatus.SYNCED
        return

    if mapping is None:
        result = client.import_data("Vouchers", codec.voucher_xml(doc, action="Create", erp_id=str(v.id)))
        if result.created < 1 and result.altered < 1:
            raise TallyError(ErrorCode.TALLY_REJECTION, "Tally accepted the request but created no voucher",
                             response_xml=client.last.response_xml if client.last else None)
        tv = _read_back(client, v, master_id=result.last_vch_id, remote_id=str(v.id))
    else:
        current = _read_back(client, v, master_id=mapping.tally_master_id)
        if current is None:
            current = _read_back(client, v, remote_id=str(v.id))
        if current is None:
            _raise_conflict(db, v, doc, mapping, {"deleted_in_tally": True}, None)
        current_hash = content_hash(current.doc)
        if current.alter_id != mapping.tally_alter_id and current_hash != mapping.last_synced_hash:
            # Tally was edited since our last sync and ERP was edited too.
            _raise_conflict(db, v, doc, mapping, current.doc, current.alter_id)
        if current_hash == v.content_hash:
            tv = current      # already identical (e.g. same edit made on both sides)
        else:
            client.import_data("Vouchers", codec.voucher_xml(doc, action="Alter", erp_id=str(v.id),
                                                              ref=_ref(conn, mapping, v.id)))
            tv = _read_back(client, v, master_id=mapping.tally_master_id)

    if tv is None:
        raise TallyError(ErrorCode.CONNECTIVITY, "Voucher written but could not be read back from Tally",
                         outcome_unknown=True)
    tally_hash = content_hash(tv.doc)
    common.save_mapping(db, conn, "voucher", v.id, guid=tv.guid, master_id=tv.master_id, alter_id=tv.alter_id,
                        content_hash=tally_hash, erp_version=v.version, direction="outbound",
                        voucher_type=doc["voucher_type"], voucher_number=v.voucher_number, voucher_date=v.voucher_date,
                        mapping=mapping)
    if tally_hash != v.content_hash:
        changes = diff(doc, tv.doc)
        raise TallyError(ErrorCode.VERIFY_MISMATCH,
                         "Tally saved the voucher but its content differs from ERP: "
                         + "; ".join(f"{c['field']}: ERP={c['erp']} Tally={c['tally']}" for c in changes[:5]),
                         response_xml=tv.raw_xml)
    v.sync_status = SyncStatus.SYNCED


def _raise_conflict(db: Session, v: Voucher, doc: dict, mapping: TallyMapping, tally_doc: dict, alter_id) -> None:
    base = None
    if mapping.erp_version_synced:
        base_row = db.scalar(select(VoucherVersion).where(
            VoucherVersion.voucher_id == v.id, VoucherVersion.version == mapping.erp_version_synced))
        base = base_row.snapshot if base_row else None
    common.open_conflict(db, v.company_id, v.id, detected_by="outbound", erp_doc=doc, tally_doc=tally_doc,
                         tally_guid=mapping.tally_guid, tally_alter_id=alter_id, base_doc=base)
    v.sync_status = SyncStatus.CONFLICT
    raise Conflict("Voucher changed in both ERP and Tally since last sync")


# ------------------------------------------------------------------ ledgers

def _sync_ledger(db: Session, ev: SyncOutbox, conn: TallyConnector, client: TallyClient) -> None:
    led = db.get(Ledger, ev.entity_id)
    mapping = common.mapping_for_erp(db, led.company_id, "ledger", led.id)
    doc = ledger_doc(led)
    doc["applicable_from"] = db.get(Company, led.company_id).books_from   # start date of dated details
    fetch = ["NAME", "GUID", "MASTERID", "ALTERID"]
    if mapping is None:
        action = "Alter" if client.masters("Ledger", fetch, name=led.name) else "Create"
    else:
        doc["old_name"] = mapping.voucher_number
        action = "Alter"
    client.import_data("All Masters", codec.ledger_xml(doc, action=action))
    after = next(iter(client.masters("Ledger", fetch, name=led.name)), None)
    if after is None:
        raise TallyError(ErrorCode.VERIFY_MISMATCH, f"Ledger '{led.name}' not found in Tally after write")
    common.save_mapping(db, conn, "ledger", led.id, guid=after["guid"], master_id=after["master_id"],
                        alter_id=after["alter_id"], content_hash=None, erp_version=led.version,
                        direction="outbound", voucher_number=led.name, mapping=mapping)
