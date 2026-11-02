"""Tally -> ERP: incremental polling by AlterID, durable inbox, idempotent apply (PRD §6.4)."""
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
    Company, SyncInbox, SyncStatus, TallyConnector, TallyMapping, Voucher, VoucherStatus, VoucherType, VoucherVersion,
)
from app.models.base import utcnow
from app.services.accounting import vouchers as engine
from app.services.canonical import content_hash
from app.services.sync import common
from app.services.sync.masters_in import MasterImporter
from app.services.sync.progress import NoProgress

log = logging.getLogger("sync.inbound")
S = VoucherStatus


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

    if counters["alt_mst_id"] is None or counters["alt_mst_id"] > conn.master_watermark:
        importer = MasterImporter(db, company, conn)
        importer.pull(client, since_alter_id=max(0, conn.master_watermark - settings.sync_inbound_overlap) or None,
                      on_type=lambda i, n, label: progress.step(1, f"Reading {label}", done=i, total=n))
        stats["masters"] = importer.counts
        conn.master_watermark = max(conn.master_watermark, importer.max_alter_id)
        db.commit()
        changed = sum(c["created"] + c["updated"] for c in importer.counts.values())
        progress.step(1, f"{changed} master(s) created or updated", state="done")
    else:
        stats["masters"] = "unchanged"
        progress.step(1, "No master changed in Tally", state="done")

    if counters["alt_vch_id"] is not None and counters["alt_vch_id"] <= conn.voucher_watermark:
        stats["vouchers"] = "unchanged"
        progress.step(2, "No voucher changed in Tally", state="done")
    elif not force and not conn.voucher_backlog and conn.voucher_scan_at and \
            (wait := settings.sync_voucher_scan_seconds - (utcnow() - conn.voucher_scan_at).total_seconds()) > 0:
        stats["vouchers"] = f"changed; next scan in {int(wait)} s"
        progress.step(2, f"Vouchers changed in Tally; next scan in {int(wait) // 60 + 1} min (Sync now scans at once)",
                      state="skipped")
    else:
        _pull_vouchers(db, company, conn, client, counters["alt_vch_id"], stats, progress)
    conn.last_inbound_at = utcnow()
    db.commit()
    return stats


def _pull_vouchers(db: Session, company: Company, conn: TallyConnector, client: TallyClient,
                   counter: int | None, stats: dict, progress) -> None:
    conn.voucher_scan_at = utcnow()
    db.commit()
    since = max(0, conn.voucher_watermark - settings.sync_inbound_overlap)
    start = voucher_from(company, conn)
    progress.step(2, "Asking Tally which vouchers changed (this takes about 15 s on a large company)")
    index = client.voucher_index(f"$AlterID > {since}")
    # Oldest changes first (by AlterID), so everything below the first change left over has been read
    # and the watermark can move up after every pass; each later scan of a backlog is then smaller.
    wanted = sorted(_unseen(db, company.id, [(r.master_id, r.alter_id) for r in index if not r.date or r.date >= start]),
                    key=lambda c: c[1])
    limit = settings.sync_voucher_max_batches * settings.sync_voucher_batch_size
    wanted, rest = wanted[:limit], wanted[limit:]
    stats["scanned"], stats["to_fetch"], stats["remaining"] = len(index), len(wanted) + len(rest), len(rest)
    frozen = set(conn.frozen_voucher_types or [])
    high = rest[0][1] - 1 if rest else max([conn.voucher_watermark, counter or 0, *(r.alter_id for r in index)])
    held = None
    done, total = 0, len(wanted)
    batches = -(-total // max(1, settings.sync_voucher_batch_size))
    progress.step(2, f"{total} changed voucher(s) to fetch" + (f" ({stats['remaining']} more on later syncs)" if stats["remaining"] else ""),
                  done=0, total=total)
    for n, batch in enumerate(client.vouchers_in_batches([m for m, _ in wanted], extra=f"$AlterID > {since}"), 1):
        done += len(batch)
        progress.step(2, f"Fetched batch {n} of {batches} from Tally; saving", done=done, total=total)
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


def _unseen(db: Session, company_id: uuid.UUID, changes: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Drop (MasterID, AlterID) pairs already received or already written by us (the overlap re-reads them)."""
    if not changes:
        return []
    mids = {m for m, _ in changes}
    known = set(db.execute(select(SyncInbox.tally_master_id, SyncInbox.tally_alter_id).where(
        SyncInbox.company_id == company_id, SyncInbox.entity_type == "voucher", SyncInbox.tally_master_id.in_(mids))))
    known |= set(db.execute(select(TallyMapping.tally_master_id, TallyMapping.tally_alter_id).where(
        TallyMapping.company_id == company_id, TallyMapping.entity_type == "voucher",
        TallyMapping.tally_master_id.in_(mids))))
    return [c for c in changes if c not in known]


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
