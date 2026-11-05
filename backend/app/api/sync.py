"""Sync dashboard, exception handling and conflict resolution (PRD §6.5, §6.6)."""
import csv
import io
import ipaddress
import logging
import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select

from app import audit
from app.api.deps import DB, CurrentUser, actor_of, authorize
from app.connectors.tally import TallyError
from app.core.db import SessionLocal
from app.core.security import Perm
from app.licensing import enforce as licence
from app.models import (
    Company, ImportBatch, OutboxStatus, ReconciliationRun, SyncAttempt, SyncConflict, SyncInbox, SyncOutbox,
    SyncProgress, TallyConnector, TallyMapping, Voucher, VoucherVersion,
)
from app.models.base import utcnow
from app.services import canonical
from app.services.accounting import masters
from app.services.accounting.vouchers import VoucherError
from app.services.sync import common, conflicts, inbound, initial_import, progress, reconcile
from app.workers.sync_worker import check_tally, run_company

log = logging.getLogger("api.sync")
router = APIRouter(tags=["sync"])


class ConnectorIn(BaseModel):
    host: str = "localhost"
    port: int = 9000
    tally_company_name: str
    tally_company_guid: str | None = None
    tdl_version: str | None = None
    alter_by: Literal["masterid", "guid", "remoteid"] = "masterid"
    enabled: bool = False
    outbound_enabled: bool = False
    inbound_enabled: bool = True
    freeze_outbound: bool = False
    freeze_inbound: bool = False
    frozen_voucher_types: list[str] = []
    inbound_from_date: date | None = None

    @field_validator("host")
    @classmethod
    def _private_only(cls, host: str) -> str:
        # PRD §6.2: never point the connector at a publicly exposed Tally listener.
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return host
        if ip.is_global:
            raise ValueError("Tally host must be on a private network or VPN, not a public IP")
        return host


def _conn_out(conn: TallyConnector) -> dict:
    return {c.name: getattr(conn, c.name) for c in TallyConnector.__table__.columns}


def _connector(db, company_id) -> TallyConnector:
    conn = db.get(TallyConnector, company_id)
    if conn is None:
        raise HTTPException(404, "No Tally connector configured for this company")
    return conn


@router.get("/sync/connectors/{company_id}")
def get_connector(company_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.SYNC_READ)
    return _conn_out(_connector(db, company_id))


@router.put("/sync/connectors/{company_id}")
def put_connector(company_id: uuid.UUID, body: ConnectorIn, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.SYNC_MANAGE)
    conn = db.get(TallyConnector, company_id)
    before = _conn_out(conn) if conn else None
    if conn is None:
        conn = TallyConnector(company_id=company_id, tally_company_name=body.tally_company_name)
        db.add(conn)
    for k, v in body.model_dump().items():
        setattr(conn, k, v)
    db.flush()
    audit.record(db, actor_of(user), "sync.connector.update", "company", company_id, company_id=company_id,
                 before=before, after=_conn_out(conn))
    db.commit()
    return _conn_out(conn)


class FreezeIn(BaseModel):
    freeze_outbound: bool | None = None
    freeze_inbound: bool | None = None
    frozen_voucher_types: list[str] | None = None
    reason: str | None = None


@router.post("/sync/connectors/{company_id}/freeze")
def freeze(company_id: uuid.UUID, body: FreezeIn, db: DB, user: CurrentUser):
    """Emergency freeze per company, direction or voucher type (PRD §6.5)."""
    authorize(db, user, company_id, Perm.SYNC_MANAGE)
    conn = _connector(db, company_id)
    changes = body.model_dump(exclude_none=True, exclude={"reason"})
    for k, v in changes.items():
        setattr(conn, k, v)
    audit.record(db, actor_of(user), "sync.freeze", "company", company_id, company_id=company_id, after=changes,
                 reason=body.reason)
    db.commit()
    return _conn_out(conn)


@router.post("/sync/connectors/{company_id}/test")
def test_connector(company_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.SYNC_READ)
    conn = _connector(db, company_id)
    client = common.client_for(conn)
    try:
        return initial_import.discover(client)
    except TallyError as exc:
        raise HTTPException(502, f"{exc.code}: {exc.message}")
    finally:
        client.close()


@router.post("/sync/connectors/{company_id}/check")
def check_connector(company_id: uuid.UUID, db: DB, user: CurrentUser):
    """Is Tally reachable, and is the linked company open in it (same name and GUID)?"""
    authorize(db, user, company_id, Perm.SYNC_READ)
    conn = _connector(db, company_id)
    row = db.get(SyncProgress, company_id)
    if row and progress.is_running(row.data):
        # Tally answers one request at a time; a check now would only queue behind the sync.
        step = next((s for s in row.data["steps"] if s["state"] == "running"), None)
        return {"server_running": True, "open_companies": [], "matched": conn.health_status == "ok",
                "tally_company_name": conn.tally_company_name, "status": "sync_running",
                "message": "A sync is running" + (f" ({step['name']}: {step['detail']})" if step else "")
                           + ". Tally answers one request at a time, so it was not asked again; see the Sync tab.",
                "checked_at": row.updated_at}
    result = check_tally(db, conn)
    db.commit()
    return result


def _run_in_background(company_id: uuid.UUID, who: str) -> None:
    try:
        run_company(SessionLocal, company_id, force=True, trigger=f"Sync now by {who}")
    except Exception:   # already recorded on the progress row; nothing to return to
        log.exception("sync run failed for %s", company_id)


@router.post("/sync/run", status_code=202)
def run_now(company_id: uuid.UUID, db: DB, user: CurrentUser, background: BackgroundTasks):
    """Start a sync now; follow it with GET /sync/progress."""
    authorize(db, user, company_id, Perm.SYNC_MANAGE)
    _connector(db, company_id)
    row = db.get(SyncProgress, company_id)
    if row and progress.is_running(row.data):
        raise HTTPException(409, "A sync is already running for this company")
    background.add_task(_run_in_background, company_id, user.username)
    return {"status": "started", "progress": f"/api/v1/sync/progress?company_id={company_id}"}


@router.get("/sync/progress")
def sync_progress(company_id: uuid.UUID, db: DB, user: CurrentUser):
    """Live progress of the current sync, or the result of the last one."""
    authorize(db, user, company_id, Perm.SYNC_READ)
    row = db.get(SyncProgress, company_id)
    if row is None:
        return {"state": "never", "steps": []}
    data = dict(row.data)
    if data.get("state") == "running" and not progress.is_running(data):
        data["state"], data["summary"] = "failed", "Stopped responding (the server or worker was restarted)"
    return data


@router.get("/sync/dashboard")
def dashboard(company_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.SYNC_READ)
    conn = db.get(TallyConnector, company_id)

    def outbox_count(*statuses):
        return db.scalar(select(func.count()).select_from(SyncOutbox).where(
            SyncOutbox.company_id == company_id, SyncOutbox.status.in_(statuses)))

    oldest = db.scalar(select(func.min(SyncOutbox.created_at)).where(
        SyncOutbox.company_id == company_id, SyncOutbox.status.in_([OutboxStatus.PENDING, OutboxStatus.PROCESSING])))
    by_code = db.execute(select(SyncOutbox.error_code, func.count()).where(
        SyncOutbox.company_id == company_id, SyncOutbox.status == OutboxStatus.FAILED).group_by(SyncOutbox.error_code))
    last_rec = db.scalar(select(ReconciliationRun).where(ReconciliationRun.company_id == company_id)
                         .order_by(ReconciliationRun.started_at.desc()).limit(1))
    from app.workers.auto_sync import auto_sync
    return {
        "auto_sync": auto_sync.status(),
        "connector": _conn_out(conn) if conn else None,
        "pending": outbox_count(OutboxStatus.PENDING, OutboxStatus.PROCESSING),
        "failed": outbox_count(OutboxStatus.FAILED),
        "outbound_conflicts": outbox_count(OutboxStatus.CONFLICT),
        "open_conflicts": db.scalar(select(func.count()).select_from(SyncConflict).where(
            SyncConflict.company_id == company_id, SyncConflict.status == "open")),
        "inbox_failed": db.scalar(select(func.count()).select_from(SyncInbox).where(
            SyncInbox.company_id == company_id, SyncInbox.status == "failed")),
        "oldest_pending_at": oldest,
        "oldest_pending_seconds": int((utcnow() - oldest).total_seconds()) if oldest else None,
        "mapped_vouchers": (synced := db.scalar(select(func.count()).select_from(TallyMapping).where(
            TallyMapping.company_id == company_id, TallyMapping.entity_type == "voucher"))),
        "vouchers": _voucher_counts(db, company_id, conn, synced),
        "failed_by_error": {code or "unknown": n for code, n in by_code},
        "last_reconciliation": {"status": last_rec.status, "differences": last_rec.differences,
                                "at": last_rec.finished_at} if last_rec else None,
    }


def _voucher_counts(db, company_id, conn, synced: int) -> dict:
    """How many vouchers Tally has, how many are synced, and how many were amended in Tally and synced.

    Tally reports no plain voucher count, so the total is what the connector knows: vouchers already
    linked, plus those received but not yet stored, plus the changed vouchers still waiting in Tally.
    It equals Tally's count once the backlog is cleared (vouchers deleted in Tally are not counted).
    """
    unmapped = db.scalar(select(func.count(func.distinct(SyncInbox.tally_master_id))).where(
        SyncInbox.company_id == company_id, SyncInbox.entity_type == "voucher", SyncInbox.status == "failed",
        ~SyncInbox.tally_master_id.in_(select(TallyMapping.tally_master_id).where(
            TallyMapping.company_id == company_id, TallyMapping.entity_type == "voucher",
            TallyMapping.tally_master_id.is_not(None)))))
    amended = db.scalar(select(func.count(func.distinct(VoucherVersion.voucher_id))).join(Voucher).where(
        Voucher.company_id == company_id, VoucherVersion.source_system == "tally", VoucherVersion.version > 1))
    backlog = conn.voucher_backlog if conn else 0
    return {"in_tally": synced + unmapped + backlog, "synced": synced, "not_stored": unmapped,
            "waiting_in_tally": backlog, "amended_in_tally": amended}


class AutoSyncIn(BaseModel):
    enabled: bool


@router.put("/sync/auto")
def set_auto_sync(body: AutoSyncIn, db: DB, user: CurrentUser):
    """Switch automatic sync on or off for this server (all companies)."""
    if not user.is_superadmin:
        raise HTTPException(403, "Only a super admin can switch automatic sync")
    from app.workers.auto_sync import auto_sync
    auto_sync.set_paused(not body.enabled)
    audit.record(db, actor_of(user), "sync.auto", "server", None, after={"enabled": body.enabled})
    db.commit()
    return auto_sync.status()


@router.get("/sync/amendments")
def amendments(company_id: uuid.UUID, db: DB, user: CurrentUser, limit: int = 50):
    """Vouchers amended in Tally whose new version has been synced, newest first, with what changed."""
    authorize(db, user, company_id, Perm.SYNC_READ)
    rows = db.execute(select(VoucherVersion, Voucher).join(Voucher).where(
        Voucher.company_id == company_id, VoucherVersion.source_system == "tally", VoucherVersion.version > 1)
        .order_by(VoucherVersion.created_at.desc()).limit(min(limit, 500))).all()
    out = []
    for ver, v in rows:
        prev = db.scalar(select(VoucherVersion).where(VoucherVersion.voucher_id == v.id,
                                                      VoucherVersion.version == ver.version - 1))
        changes = [d["field"] for d in canonical.diff(prev.snapshot, ver.snapshot)] if prev else []
        if prev and prev.status != ver.status:
            changes.insert(0, "status")
        out.append({"voucher_id": v.id, "at": ver.created_at, "version": ver.version, "voucher_type": v.voucher_type.name,
                    "voucher_number": v.voucher_number, "date": v.voucher_date, "status": ver.status,
                    "changes": changes})
    return out


# ------------------------------------------------------------------ outbound jobs

def _job_out(ev: SyncOutbox) -> dict:
    return {c.name: getattr(ev, c.name) for c in SyncOutbox.__table__.columns}


@router.get("/sync/jobs")
def list_jobs(company_id: uuid.UUID, db: DB, user: CurrentUser, status: str | None = None, error_code: str | None = None,
              voucher_type: str | None = None, voucher_number: str | None = None, limit: int = 100, offset: int = 0):
    authorize(db, user, company_id, Perm.SYNC_READ)
    stmt = select(SyncOutbox).where(SyncOutbox.company_id == company_id)
    for col, val in ((SyncOutbox.status, status), (SyncOutbox.error_code, error_code),
                     (SyncOutbox.voucher_type, voucher_type), (SyncOutbox.voucher_number, voucher_number)):
        if val:
            stmt = stmt.where(col == val)
    stmt = stmt.order_by(SyncOutbox.created_at.desc()).limit(min(limit, 1000)).offset(offset)
    return [_job_out(ev) for ev in db.scalars(stmt)]


def _job(db, user, job_id: uuid.UUID, perm: str) -> SyncOutbox:
    ev = db.get(SyncOutbox, job_id)
    if ev is None:
        raise HTTPException(404, "Job not found")
    authorize(db, user, ev.company_id, perm)
    return ev


@router.get("/sync/jobs/{job_id}")
def get_job(job_id: uuid.UUID, db: DB, user: CurrentUser):
    ev = _job(db, user, job_id, Perm.SYNC_READ)
    attempts = db.scalars(select(SyncAttempt).where(SyncAttempt.outbox_id == ev.id).order_by(SyncAttempt.attempted_at))
    return {**_job_out(ev), "attempts": [
        {"at": a.attempted_at, "success": a.success, "error_code": a.error_code, "error": a.error,
         "duration_ms": a.duration_ms, "request_xml": a.request_xml, "response_xml": a.response_xml} for a in attempts]}


@router.post("/sync/jobs/{job_id}/retry")
def retry_job(job_id: uuid.UUID, db: DB, user: CurrentUser):
    ev = _job(db, user, job_id, Perm.SYNC_MANAGE)
    if ev.status not in (OutboxStatus.FAILED, OutboxStatus.PAUSED, OutboxStatus.PENDING):
        raise HTTPException(409, f"A {ev.status} job cannot be retried")
    ev.status, ev.next_attempt_at, ev.attempts = OutboxStatus.PENDING, utcnow(), 0
    audit.record(db, actor_of(user), "sync.job.retry", "sync_job", ev.id, company_id=ev.company_id)
    db.commit()
    return _job_out(ev)


@router.post("/sync/jobs/{job_id}/{action}")
def pause_resume(job_id: uuid.UUID, action: Literal["pause", "resume"], db: DB, user: CurrentUser):
    ev = _job(db, user, job_id, Perm.SYNC_MANAGE)
    if action == "pause" and ev.status == OutboxStatus.PENDING:
        ev.status = OutboxStatus.PAUSED
    elif action == "resume" and ev.status == OutboxStatus.PAUSED:
        ev.status, ev.next_attempt_at = OutboxStatus.PENDING, utcnow()
    else:
        raise HTTPException(409, f"Cannot {action} a {ev.status} job")
    audit.record(db, actor_of(user), f"sync.job.{action}", "sync_job", ev.id, company_id=ev.company_id)
    db.commit()
    return _job_out(ev)


@router.post("/sync/retry-failed")
def retry_failed(company_id: uuid.UUID, db: DB, user: CurrentUser, error_code: str | None = None):
    """Bulk 'reprocess after master creation'."""
    authorize(db, user, company_id, Perm.SYNC_MANAGE)
    stmt = select(SyncOutbox).where(SyncOutbox.company_id == company_id, SyncOutbox.status == OutboxStatus.FAILED)
    if error_code:
        stmt = stmt.where(SyncOutbox.error_code == error_code)
    jobs = db.scalars(stmt).all()
    for ev in jobs:
        ev.status, ev.next_attempt_at, ev.attempts = OutboxStatus.PENDING, utcnow(), 0
    items = db.scalars(select(SyncInbox).where(SyncInbox.company_id == company_id, SyncInbox.status == "failed")).all()
    outcomes = [inbound.apply_item(db, db.get(Company, company_id), db.get(TallyConnector, company_id), i) for i in items]
    audit.record(db, actor_of(user), "sync.retry_failed", "company", company_id, company_id=company_id,
                 after={"outbound": len(jobs), "inbound": len(items)})
    db.commit()
    return {"outbound_requeued": len(jobs), "inbound_reprocessed": len(items),
            "inbound_outcomes": {o: outcomes.count(o) for o in set(outcomes)}}


@router.get("/sync/errors.csv")
def export_errors(company_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.EXPORT)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["direction", "id", "status", "error_code", "error", "voucher_type", "voucher_number", "attempts", "created_at"])
    for ev in db.scalars(select(SyncOutbox).where(SyncOutbox.company_id == company_id,
                                                  SyncOutbox.status.in_([OutboxStatus.FAILED, OutboxStatus.CONFLICT]))):
        w.writerow(["outbound", ev.id, ev.status, ev.error_code, ev.last_error, ev.voucher_type, ev.voucher_number,
                    ev.attempts, ev.created_at])
    for it in db.scalars(select(SyncInbox).where(SyncInbox.company_id == company_id,
                                                 SyncInbox.status.in_(["failed", "conflict"]))):
        w.writerow(["inbound", it.id, it.status, it.error_code, it.error, it.voucher_type, it.voucher_number,
                    it.attempts, it.received_at])
    audit.record(db, actor_of(user), "sync.errors.export", "company", company_id, company_id=company_id)
    db.commit()
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="sync-errors.csv"'})


# ------------------------------------------------------------------ inbound

@router.get("/sync/inbox")
def list_inbox(company_id: uuid.UUID, db: DB, user: CurrentUser, status: str | None = None, limit: int = 100, offset: int = 0):
    authorize(db, user, company_id, Perm.SYNC_READ)
    stmt = select(SyncInbox).where(SyncInbox.company_id == company_id)
    if status:
        stmt = stmt.where(SyncInbox.status == status)
    stmt = stmt.order_by(SyncInbox.received_at.desc()).limit(min(limit, 1000)).offset(offset)
    return [{c.name: getattr(i, c.name) for c in SyncInbox.__table__.columns if c.name != "raw_xml"} for i in db.scalars(stmt)]


@router.post("/sync/inbox/{item_id}/reprocess")
def reprocess_inbox(item_id: uuid.UUID, db: DB, user: CurrentUser):
    item = db.get(SyncInbox, item_id)
    if item is None:
        raise HTTPException(404, "Inbox item not found")
    authorize(db, user, item.company_id, Perm.SYNC_MANAGE)
    outcome = inbound.reprocess(db, item)
    return {"outcome": outcome, "error_code": item.error_code, "error": item.error, "erp_id": item.erp_id}


# ------------------------------------------------------------------ conflicts

def _conflict_out(c: SyncConflict) -> dict:
    return {col.name: getattr(c, col.name) for col in SyncConflict.__table__.columns}


@router.get("/sync/conflicts")
def list_conflicts(company_id: uuid.UUID, db: DB, user: CurrentUser, status: str = "open"):
    authorize(db, user, company_id, Perm.SYNC_READ)
    rows = db.scalars(select(SyncConflict).where(SyncConflict.company_id == company_id, SyncConflict.status == status)
                      .order_by(SyncConflict.created_at.desc()))
    return [_conflict_out(c) for c in rows]


@router.get("/sync/conflicts/{conflict_id}")
def get_conflict(conflict_id: uuid.UUID, db: DB, user: CurrentUser):
    c = db.get(SyncConflict, conflict_id)
    if c is None:
        raise HTTPException(404, "Conflict not found")
    authorize(db, user, c.company_id, Perm.SYNC_READ)
    v = db.get(Voucher, c.erp_id)
    return {**_conflict_out(c), "erp_updated_by": v.updated_by, "erp_updated_at": v.updated_at, "erp_version": v.version}


class ResolveIn(BaseModel):
    choice: Literal["erp", "tally", "merge"]
    merged: dict | None = None
    reason: str | None = None


@router.post("/sync/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: uuid.UUID, body: ResolveIn, db: DB, user: CurrentUser):
    c = db.get(SyncConflict, conflict_id)
    if c is None:
        raise HTTPException(404, "Conflict not found")
    authorize(db, user, c.company_id, Perm.CONFLICT_RESOLVE)
    v = conflicts.resolve(db, c, body.choice, actor_of(user), merged=body.merged, reason=body.reason)
    db.commit()
    return {"conflict_id": c.id, "status": c.status, "voucher_id": v.id, "voucher_version": v.version,
            "sync_status": v.sync_status}


# ------------------------------------------------------------------ reconciliation & mappings

class ReconcileIn(BaseModel):
    company_id: uuid.UUID
    from_date: date
    to_date: date
    compare_ledgers: bool = True


@router.post("/sync/reconcile")
def run_reconcile(body: ReconcileIn, db: DB, user: CurrentUser):
    company = authorize(db, user, body.company_id, Perm.SYNC_MANAGE)
    conn = _connector(db, body.company_id)
    client = common.client_for(conn)
    try:
        run = reconcile.run(db, company, conn, client, body.from_date, body.to_date, actor_of(user),
                            compare_ledgers=body.compare_ledgers)
    finally:
        client.close()
    db.commit()
    return {"id": run.id, "status": run.status, "differences": run.differences, "result": run.result}


@router.get("/sync/reconciliations")
def list_reconciliations(company_id: uuid.UUID, db: DB, user: CurrentUser, limit: int = 20):
    authorize(db, user, company_id, Perm.SYNC_READ)
    rows = db.scalars(select(ReconciliationRun).where(ReconciliationRun.company_id == company_id)
                      .order_by(ReconciliationRun.started_at.desc()).limit(limit))
    return [{c.name: getattr(r, c.name) for c in ReconciliationRun.__table__.columns} for r in rows]


@router.get("/sync/mappings")
def list_mappings(company_id: uuid.UUID, db: DB, user: CurrentUser, entity_type: str = "voucher",
                  limit: int = 100, offset: int = 0):
    authorize(db, user, company_id, Perm.SYNC_READ)
    rows = db.scalars(select(TallyMapping).where(TallyMapping.company_id == company_id,
                                                 TallyMapping.entity_type == entity_type)
                      .order_by(TallyMapping.updated_at.desc()).limit(min(limit, 1000)).offset(offset))
    return [{c.name: getattr(r, c.name) for c in TallyMapping.__table__.columns} for r in rows]


# ------------------------------------------------------------------ import / onboarding

class ImportIn(BaseModel):
    company_id: uuid.UUID
    masters: bool = True
    vouchers: bool = False
    from_date: date | None = None
    to_date: date | None = None
    batch_size: int | None = None   # vouchers per Tally request (default: ERP_SYNC_VOUCHER_BATCH_SIZE)


def _run_import(company_id: uuid.UUID, body: ImportIn, actor) -> None:
    with SessionLocal() as db:
        company, conn = db.get(Company, company_id), db.get(TallyConnector, company_id)
        client = common.client_for(conn)
        try:
            if body.masters:
                initial_import.import_masters(db, company, conn, client, actor)
            if body.vouchers:
                initial_import.import_vouchers(db, company, conn, client, actor, body.from_date or company.books_from,
                                               body.to_date or date.today(), body.batch_size)
        finally:
            client.close()


@router.post("/import/tally", status_code=202)
def start_import(body: ImportIn, background: BackgroundTasks, db: DB, user: CurrentUser):
    """Start a controlled import; progress is visible under /import/batches."""
    authorize(db, user, body.company_id, Perm.SYNC_MANAGE)
    _connector(db, body.company_id)
    background.add_task(_run_import, body.company_id, body, actor_of(user))
    audit.record(db, actor_of(user), "import.start", "company", body.company_id, company_id=body.company_id,
                 after=body.model_dump())
    db.commit()
    return {"status": "started", "check": f"/api/v1/import/batches?company_id={body.company_id}"}


@router.get("/import/batches")
def list_batches(company_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.SYNC_READ)
    rows = db.scalars(select(ImportBatch).where(ImportBatch.company_id == company_id).order_by(ImportBatch.started_at.desc()).limit(50))
    return [{c.name: getattr(b, c.name) for c in ImportBatch.__table__.columns} for b in rows]


class DiscoverIn(BaseModel):
    host: str = "localhost"
    port: int = 9000


@router.post("/import/tally/discover")
def discover(body: DiscoverIn, db: DB, user: CurrentUser):
    """List companies open in a Tally instance (read-only), marking those already linked to an ERP company."""
    if not user.is_superadmin:
        raise HTTPException(403, "Only a super admin can probe Tally servers")
    ConnectorIn(host=body.host, tally_company_name="-")   # same private-network rule
    from app.connectors.tally import TallyClient
    with TallyClient(body.host, body.port, "") as client:
        try:
            server, companies = client.ping(), client.list_companies()
        except TallyError as exc:
            raise HTTPException(502, f"{exc.code}: {exc.message}")
    linked = {c.tally_company_guid: c.company_id for c in db.scalars(select(TallyConnector))
              if c.tally_company_guid}
    for c in companies:
        c["erp_company_id"] = linked.get(c["guid"])
    return {"server": server, "companies": companies}


class LinkIn(BaseModel):
    host: str = "localhost"
    port: int = 9000
    tally_company_name: str
    erp_company_name: str | None = None
    import_masters: bool = True


@router.post("/import/tally/link", status_code=201)
def link_company(body: LinkIn, background: BackgroundTasks, db: DB, user: CurrentUser):
    """Create an ERP company for a Tally company in shadow mode (inbound only; outbound off)."""
    if not user.is_superadmin:
        raise HTTPException(403, "Only a super admin can create companies")
    ConnectorIn(host=body.host, tally_company_name=body.tally_company_name)
    from app.connectors.tally import TallyClient
    with TallyClient(body.host, body.port, body.tally_company_name) as client:
        try:
            found = next((c for c in client.list_companies() if c["name"] == body.tally_company_name), None)
        except TallyError as exc:
            raise HTTPException(502, f"{exc.code}: {exc.message}")
    if found is None:
        raise HTTPException(404, f"'{body.tally_company_name}' is not open in Tally")
    licence.check_company_limit(db)
    existing = db.scalar(select(TallyConnector).where(TallyConnector.tally_company_guid == found["guid"]))
    if existing is not None:
        raise HTTPException(409, f"'{body.tally_company_name}' is already linked to ERP company {existing.company_id}")
    books_from = date.fromisoformat(found["books_from"] or found["starting_from"])
    try:
        company = masters.create_company(db, {"name": body.erp_company_name or body.tally_company_name,
                                              "books_from": books_from}, actor_of(user), with_defaults=False)
    except VoucherError as exc:
        raise HTTPException(409, exc.message)
    db.add(TallyConnector(company_id=company.id, host=body.host, port=body.port,
                          tally_company_name=body.tally_company_name, tally_company_guid=found["guid"],
                          enabled=True, inbound_enabled=True, outbound_enabled=False))
    db.commit()
    if body.import_masters:
        background.add_task(_run_import, company.id, ImportIn(company_id=company.id), actor_of(user))
    return {"company_id": company.id, "books_from": books_from, "tally_company_guid": found["guid"],
            "mode": "shadow (inbound only) – enable outbound after reconciliation sign-off"}
