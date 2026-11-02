"""Authorized conflict resolution: keep ERP, accept Tally, or merge (PRD §6.5)."""
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import audit
from app.audit import Actor
from app.models import OutboxStatus, SyncConflict, SyncOutbox, SyncStatus, TallyConnector, Voucher
from app.models.base import utcnow
from app.services.accounting import vouchers as engine
from app.services.canonical import content_hash
from app.services.sync import common
from app.services.sync.inbound import apply_tally_doc


class ResolutionError(engine.VoucherError):
    status_code = 409


def resolve(db: Session, conflict: SyncConflict, choice: str, actor: Actor, *, merged: dict | None = None,
            reason: str | None = None) -> Voucher:
    if conflict.status != "open":
        raise ResolutionError("validation_failure", "Conflict is already resolved")
    v = db.get(Voucher, conflict.erp_id)
    conn = db.get(TallyConnector, conflict.company_id)
    mapping = common.mapping_for_erp(db, conflict.company_id, "voucher", v.id)
    tally_doc = conflict.tally_snapshot or {}
    tally_deleted = bool(tally_doc.get("deleted_in_tally"))
    note = reason or f"Conflict resolved: {choice}"

    # Anything still queued for this voucher was produced before the decision.
    db.execute(update(SyncOutbox).where(
        SyncOutbox.entity_type == "voucher", SyncOutbox.entity_id == v.id,
        SyncOutbox.status.in_([OutboxStatus.PENDING, OutboxStatus.FAILED, OutboxStatus.CONFLICT]),
    ).values(status=OutboxStatus.SUPERSEDED, completed_at=utcnow()))

    if choice == "tally":
        if tally_deleted:
            engine.transition(db, v, "delete", actor, reason=note + " (deleted in Tally)", source="tally")
        else:
            apply_tally_doc(db, v, tally_doc, actor=actor, reason=note)
        common.save_mapping(db, conn, "voucher", v.id, guid=mapping.tally_guid, master_id=mapping.tally_master_id,
                            alter_id=conflict.tally_alter_id, content_hash=v.content_hash, erp_version=v.version,
                            direction="inbound", mapping=mapping)
        v.sync_status = SyncStatus.SYNCED
    elif choice in ("erp", "merge"):
        # Accept Tally's current state as the new common base, then push ERP's (or the merged) version.
        if tally_deleted:
            raise ResolutionError("validation_failure",
                                  "The voucher was deleted in Tally; accept the Tally version or re-enter it in ERP")
        mapping.tally_alter_id = conflict.tally_alter_id
        mapping.last_synced_hash = content_hash(tally_doc)
        if choice == "merge":
            if not merged:
                raise ResolutionError("validation_failure", "A merge needs the merged voucher content")
            engine.update_voucher(db, v, merged, actor, expected_version=None, source="erp", reason=note)
        else:
            engine.bump_version(db, v, actor, note)
            engine.enqueue_outbound(db, v, actor, "upsert")
    else:
        raise ResolutionError("validation_failure", "choice must be erp, tally or merge")

    for c in db.scalars(select(SyncConflict).where(SyncConflict.erp_id == v.id, SyncConflict.status == "open")):
        c.status, c.resolution, c.resolved_by, c.resolved_at = "resolved", choice, actor.name, utcnow()
    audit.record(db, actor, "sync.conflict.resolve", "voucher", v.id, company_id=v.company_id,
                 before={"erp": conflict.erp_snapshot, "tally": tally_doc}, after={"choice": choice, "version": v.version},
                 reason=note)
    return v
