"""Push / pull history: who pulled from or pushed to Tally, when, and what moved.

One row per Pull or Push someone started (all its parts together), and one per automatic run that moved
something (an automatic run every few seconds that found nothing new is not kept). The vouchers a run
moved are not copied: they are the inbox rows received and the outbox attempts made in the run's time.
"""
import uuid

from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.models import SyncAttempt, SyncInbox, SyncOutbox, SyncRun, Voucher, VoucherVersion
from app.models.base import utcnow
from app.services import canonical

PULL_KEYS = ("received", "applied", "failed", "conflict", "ignored")
PUSH_KEYS = ("completed", "failed", "conflict", "retry")
LABEL = {"pull": "Pull", "push": "Push", "both": "Pull and push"}


def has_pull(direction: str) -> bool:
    return direction in ("pull", "both")


def has_push(direction: str) -> bool:
    return direction in ("push", "both")


def new_counts() -> dict:
    return {"pull": dict.fromkeys(PULL_KEYS, 0), "push": dict.fromkeys(PUSH_KEYS, 0), "masters": 0}


def add(counts: dict, stats: dict | None) -> dict:
    """Adds one pass of run_company to the run's counts."""
    stats = stats or {}
    inbound, outbound = stats.get("inbound") or {}, stats.get("outbound") or {}
    for k in PULL_KEYS:
        counts["pull"][k] += int(inbound.get(k) or 0)
    for k in PUSH_KEYS:
        counts["push"][k] += int(outbound.get(k) or 0)
    if isinstance(inbound.get("masters"), dict):
        counts["masters"] += sum(c.get("created", 0) + c.get("updated", 0) for c in inbound["masters"].values()
                                 if isinstance(c, dict))
    return counts


def moved(counts: dict) -> bool:
    return any(counts["pull"].values()) or any(counts["push"].values()) or bool(counts["masters"])


def describe(direction: str, counts: dict) -> str:
    parts = []
    if has_pull(direction):
        p = counts["pull"]
        parts.append(f"{p['applied']} voucher change(s) pulled from Tally"
                     + (f", {counts['masters']} master(s)" if counts["masters"] else ""))
        if p["failed"] or p["conflict"]:
            parts.append(f"{p['failed'] + p['conflict']} from Tally need attention")
    if has_push(direction):
        p = counts["push"]
        parts.append(f"{p['completed']} pushed to Tally")
        if p["failed"] or p["conflict"]:
            parts.append(f"{p['failed'] + p['conflict']} not accepted by Tally")
        if p["retry"]:
            parts.append(f"{p['retry']} to be tried again")
    return "; ".join(parts)


def state_of(stats: dict | None) -> str:
    stats = stats or {}
    if stats.get("stopped"):
        return "stopped"
    if stats.get("health") not in (None, "ok") or "error" in (stats.get("inbound") or {}):
        return "failed"
    return "done"


def start(company_id: uuid.UUID, direction: str, who: str, trigger: str = "manual") -> uuid.UUID:
    with SessionLocal() as db:
        run = SyncRun(company_id=company_id, direction=direction, trigger=trigger, started_by=who,
                      started_at=utcnow(), state="running", counts=new_counts())
        db.add(run)
        db.commit()
        return run.id


def finish(run_id: uuid.UUID, counts: dict, state: str, problem: str | None = None) -> None:
    with SessionLocal() as db:
        run = db.get(SyncRun, run_id)
        run.counts, run.state, run.finished_at = counts, state, utcnow()
        run.summary = "; ".join(x for x in (describe(run.direction, counts), problem) if x)
        db.commit()


def record_automatic(company_id: uuid.UUID, direction: str, started_at, stats: dict | None) -> None:
    """Automatic sync: kept only when it moved something or went wrong part-way (Tally closed is not kept)."""
    if not isinstance(stats, dict):
        return
    counts = add(new_counts(), stats)
    state = state_of(stats)
    if not moved(counts) and state != "stopped":
        return
    with SessionLocal() as db:
        run = SyncRun(company_id=company_id, direction=direction, trigger="automatic", started_by="automatic sync",
                      started_at=started_at, finished_at=utcnow(), state=state, counts=counts,
                      summary=describe(direction, counts))
        db.add(run)
        db.commit()


def problem_of(stats: dict | None) -> str | None:
    stats = stats or {}
    if (inbound := stats.get("inbound") or {}).get("error"):
        return f"pulling failed: {inbound.get('message')}"
    if stats.get("health") not in (None, "ok"):
        return f"Tally not ready: {stats.get('health_message') or stats.get('health')}"
    if stats.get("stopped"):
        return f"stopped by {stats['stopped']}"
    return None


# ------------------------------------------------------------------ reading the history

def run_out(r: SyncRun) -> dict:
    return {"id": r.id, "direction": r.direction, "label": LABEL.get(r.direction, r.direction), "trigger": r.trigger,
            "started_by": r.started_by, "started_at": r.started_at, "finished_at": r.finished_at, "state": r.state,
            "summary": r.summary, "counts": r.counts}


def runs(db, company_id: uuid.UUID, direction: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    stmt = select(SyncRun).where(SyncRun.company_id == company_id)
    if direction in ("pull", "push"):
        stmt = stmt.where(SyncRun.direction.in_([direction, "both"]))
    stmt = stmt.order_by(SyncRun.started_at.desc()).limit(min(limit, 500)).offset(offset)
    return [run_out(r) for r in db.scalars(stmt)]


MAX_ROWS = 2000


def _changes(db, voucher_id, version: VoucherVersion) -> list[str]:
    prev = db.scalar(select(VoucherVersion).where(VoucherVersion.voucher_id == voucher_id,
                                                  VoucherVersion.version == version.version - 1))
    if prev is None:
        return []
    out = [d["field"] for d in canonical.diff(prev.snapshot, version.snapshot)]
    if prev.status != version.status:
        out.insert(0, "status")
    return out


def details(db, run: SyncRun) -> dict:
    """The vouchers (and masters) a run moved, with what changed in each."""
    end = run.finished_at or utcnow()
    out = {"run": run_out(run), "pulled": [], "pushed": [], "truncated": False}
    if has_pull(run.direction):
        rows = db.scalars(select(SyncInbox).where(
            SyncInbox.company_id == run.company_id, SyncInbox.received_at >= run.started_at, SyncInbox.received_at <= end)
            .order_by(SyncInbox.received_at).limit(MAX_ROWS + 1)).all()
        out["truncated"] |= len(rows) > MAX_ROWS
        ids = [r.erp_id for r in rows[:MAX_ROWS] if r.erp_id and r.entity_type == "voucher"]
        versions = {}
        if ids:
            for ver in db.scalars(select(VoucherVersion).where(
                    VoucherVersion.voucher_id.in_(ids), VoucherVersion.source_system == "tally",
                    VoucherVersion.created_at >= run.started_at, VoucherVersion.created_at <= end)
                    .order_by(VoucherVersion.version)):
                versions[ver.voucher_id] = ver           # the newest version the run made
        for r in rows[:MAX_ROWS]:
            ver = versions.get(r.erp_id)
            action = ("new" if ver.version == 1 else "altered") if ver else \
                {"applied": "unchanged", "ignored": "ignored", "failed": "failed", "conflict": "conflict"}.get(r.status, r.status)
            if ver is not None and ver.status == "cancelled":
                action = "cancelled"
            out["pulled"].append({
                "at": r.received_at, "entity_type": r.entity_type, "voucher_type": r.voucher_type,
                "voucher_number": r.voucher_number, "date": (r.payload or {}).get("date"), "tally_alter_id": r.tally_alter_id,
                "status": r.status, "action": action, "voucher_id": r.erp_id if r.entity_type == "voucher" else None,
                "version": ver.version if ver else None,
                "changes": _changes(db, r.erp_id, ver) if ver is not None and ver.version > 1 else [],
                "error": r.error})
    if has_push(run.direction):
        rows = db.execute(select(SyncAttempt, SyncOutbox).join(SyncOutbox, SyncOutbox.id == SyncAttempt.outbox_id).where(
            SyncOutbox.company_id == run.company_id, SyncAttempt.attempted_at >= run.started_at,
            SyncAttempt.attempted_at <= end).order_by(SyncAttempt.attempted_at).limit(MAX_ROWS + 1)).all()
        out["truncated"] |= len(rows) > MAX_ROWS
        for att, ev in rows[:MAX_ROWS]:
            out["pushed"].append({
                "at": att.attempted_at, "entity_type": ev.entity_type, "operation": op_label(ev),
                "voucher_type": ev.voucher_type, "voucher_number": ev.voucher_number,
                "voucher_id": ev.entity_id if ev.entity_type == "voucher" else None, "version": ev.entity_version,
                "changed_by": ev.created_by, "result": "sent" if att.success else (att.error_code or "failed"),
                "error": att.error, "job_id": ev.id})
    return out


def op_label(ev: SyncOutbox) -> str:
    if ev.operation == "delete":
        return "delete"
    if ev.operation == "cancel":
        return "cancel"
    return "new" if ev.entity_version <= 1 else "alter"


# ------------------------------------------------------------------ waiting to be pushed

WAITING_PUSH = ("pending", "processing", "failed", "paused", "conflict")
WHY = {"pending": "Waiting for Push", "processing": "Being pushed now", "paused": "Paused",
       "failed": "Tally did not accept it", "conflict": "Changed in Tally too: resolve it in Admin > Conflicts"}


def pending_push(db, company_id: uuid.UUID, conn, limit: int = 1000) -> dict:
    """What a Push would send to Tally (and what Tally refused), oldest first."""
    rows = db.execute(select(SyncOutbox, Voucher.voucher_date, Voucher.status).outerjoin(
        Voucher, (Voucher.id == SyncOutbox.entity_id) & (SyncOutbox.entity_type == "voucher")).where(
        SyncOutbox.company_id == company_id, SyncOutbox.status.in_(WAITING_PUSH))
        .order_by(SyncOutbox.created_at).limit(min(limit, 5000))).all()
    by_status = dict(db.execute(select(SyncOutbox.status, func.count()).where(
        SyncOutbox.company_id == company_id, SyncOutbox.status.in_(WAITING_PUSH)).group_by(SyncOutbox.status)).all())
    blocked = None
    if conn is None:
        blocked = "No Tally connector"
    elif not conn.outbound_enabled:
        blocked = "Push is off: the connector is one way (Tally → software). Admin > Connector sets it."
    elif conn.freeze_outbound:
        blocked = "Push is frozen (Admin > Connector > Emergency freeze)"
    frozen = set((conn.frozen_voucher_types or []) if conn else [])
    items = []
    for ev, vdate, vstatus in rows:
        why = WHY.get(ev.status, ev.status)
        if ev.status == "pending" and ev.voucher_type in frozen:
            why = f"Voucher type {ev.voucher_type} is frozen"
        items.append({"job_id": ev.id, "entity_type": ev.entity_type, "operation": op_label(ev),
                      "voucher_type": ev.voucher_type, "voucher_number": ev.voucher_number, "date": vdate,
                      "voucher_status": vstatus, "voucher_id": ev.entity_id if ev.entity_type == "voucher" else None,
                      "version": ev.entity_version, "changed_by": ev.created_by, "changed_at": ev.created_at,
                      "status": ev.status, "attempts": ev.attempts, "why": why,
                      "error": ev.last_error if ev.status in ("failed", "conflict") else None})
    return {"total": sum(by_status.values()), "by_status": by_status, "blocked": blocked, "items": items}
