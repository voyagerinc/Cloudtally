import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, or_, select

from app.api.deps import DB, CurrentUser, actor_of, authorize
from app.core.security import Perm
from app.models import Ledger, SyncOutbox, Voucher, VoucherStatus, VoucherType, VoucherVersion
from app.schemas.vouchers import TransitionIn, VoucherCreate, VoucherOut, VoucherSummary, VoucherUpdate
from app.services import canonical
from app.services.accounting import vouchers as engine
from app.services.sync.common import mapping_for_erp

router = APIRouter(tags=["vouchers"])

ACTION_PERMS = {
    "submit": Perm.VOUCHER_WRITE, "approve": Perm.VOUCHER_APPROVE, "reject": Perm.VOUCHER_APPROVE,
    "post": Perm.VOUCHER_POST, "cancel": Perm.VOUCHER_POST, "delete": Perm.VOUCHER_DELETE,
}


def _out(db, v: Voucher) -> VoucherOut:
    mp = mapping_for_erp(db, v.company_id, "voucher", v.id)
    tally = None
    if mp:
        tally = {"guid": mp.tally_guid, "master_id": mp.tally_master_id, "alter_id": mp.tally_alter_id,
                 "last_synced_at": mp.last_synced_at, "direction": mp.sync_direction,
                 "erp_version_synced": mp.erp_version_synced}
    last = db.scalar(select(SyncOutbox).where(SyncOutbox.entity_id == v.id).order_by(SyncOutbox.created_at.desc()).limit(1))
    if last and last.last_error:
        tally = {**(tally or {}), "last_error": last.last_error, "error_code": last.error_code}
    return VoucherOut(**{k: getattr(v, k) for k in VoucherOut.model_fields if k not in ("doc", "tally")},
                      doc=engine.to_doc(v), tally=tally)


def _load(db, user, voucher_id: uuid.UUID, perm: str) -> Voucher:
    v = db.get(Voucher, voucher_id)
    if v is None:
        raise HTTPException(404, "Voucher not found")
    authorize(db, user, v.company_id, perm)
    return v


@router.post("/companies/{company_id}/vouchers", response_model=VoucherOut, status_code=201)
def create_voucher(company_id: uuid.UUID, body: VoucherCreate, db: DB, user: CurrentUser):
    company = authorize(db, user, company_id, Perm.VOUCHER_WRITE)
    if body.action == "post":
        authorize(db, user, company_id, Perm.VOUCHER_POST)
    data = body.model_dump(mode="json", exclude={"action"})
    v = engine.create_voucher(db, company, data, actor_of(user), action=body.action)
    db.commit()
    return _out(db, v)


@router.get("/companies/{company_id}/vouchers", response_model=list[VoucherSummary])
def list_vouchers(company_id: uuid.UUID, db: DB, user: CurrentUser, from_date: date | None = None,
                  to_date: date | None = None, voucher_type: str | None = None, status: str | None = None,
                  sync_status: str | None = None, q: str | None = None, limit: int = 100, offset: int = 0):
    authorize(db, user, company_id, Perm.VOUCHER_READ)
    stmt = (select(Voucher).join(VoucherType).outerjoin(Ledger, Ledger.id == Voucher.party_ledger_id)
            .where(Voucher.company_id == company_id))
    if from_date:
        stmt = stmt.where(Voucher.voucher_date >= from_date)
    if to_date:
        stmt = stmt.where(Voucher.voucher_date <= to_date)
    if voucher_type:
        stmt = stmt.where(VoucherType.name == voucher_type)
    stmt = stmt.where(Voucher.status == status) if status else stmt.where(Voucher.status != VoucherStatus.DELETED)
    if sync_status:
        stmt = stmt.where(Voucher.sync_status == sync_status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Voucher.voucher_number.ilike(like), Voucher.narration.ilike(like), Ledger.name.ilike(like),
                              Voucher.reference_no.ilike(like)))
    stmt = stmt.order_by(Voucher.voucher_date.desc(), Voucher.created_at.desc()).limit(min(limit, 1000)).offset(offset)
    return [VoucherSummary(id=v.id, voucher_type=v.voucher_type.name, voucher_number=v.voucher_number,
                           date=v.voucher_date, party_ledger=v.party_ledger.name if v.party_ledger else None,
                           narration=v.narration, amount=engine.voucher_total(v), status=v.status,
                           sync_status=v.sync_status, source_system=v.source_system, version=v.version)
            for v in db.scalars(stmt)]


@router.get("/companies/{company_id}/vouchers/count")
def count_vouchers(company_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.VOUCHER_READ)
    rows = db.execute(select(Voucher.status, Voucher.sync_status, func.count())
                      .where(Voucher.company_id == company_id).group_by(Voucher.status, Voucher.sync_status))
    return [{"status": s, "sync_status": ss, "count": n} for s, ss, n in rows]


@router.get("/vouchers/{voucher_id}", response_model=VoucherOut)
def get_voucher(voucher_id: uuid.UUID, db: DB, user: CurrentUser):
    return _out(db, _load(db, user, voucher_id, Perm.VOUCHER_READ))


@router.patch("/vouchers/{voucher_id}", response_model=VoucherOut)
def update_voucher(voucher_id: uuid.UUID, body: VoucherUpdate, db: DB, user: CurrentUser):
    v = _load(db, user, voucher_id, Perm.VOUCHER_WRITE)
    if v.status == VoucherStatus.POSTED:
        authorize(db, user, v.company_id, Perm.VOUCHER_POST)
    data = body.model_dump(mode="json", exclude={"expected_version", "reason"})
    engine.update_voucher(db, v, data, actor_of(user), expected_version=body.expected_version, reason=body.reason)
    db.commit()
    return _out(db, v)


@router.post("/vouchers/{voucher_id}/{action}", response_model=VoucherOut)
def voucher_action(voucher_id: uuid.UUID, action: Literal["submit", "approve", "reject", "post", "cancel"],
                   db: DB, user: CurrentUser, body: TransitionIn | None = None):
    v = _load(db, user, voucher_id, ACTION_PERMS[action])
    body = body or TransitionIn()
    engine.transition(db, v, action, actor_of(user), reason=body.reason, expected_version=body.expected_version)
    db.commit()
    return _out(db, v)


@router.delete("/vouchers/{voucher_id}", response_model=VoucherOut)
def delete_voucher(voucher_id: uuid.UUID, db: DB, user: CurrentUser, reason: str | None = None):
    v = _load(db, user, voucher_id, Perm.VOUCHER_DELETE)
    engine.transition(db, v, "delete", actor_of(user), reason=reason)
    db.commit()
    return _out(db, v)


@router.post("/vouchers/{voucher_id}/duplicate", response_model=VoucherOut, status_code=201)
def duplicate_voucher(voucher_id: uuid.UUID, db: DB, user: CurrentUser):
    src = _load(db, user, voucher_id, Perm.VOUCHER_WRITE)
    company = authorize(db, user, src.company_id, Perm.VOUCHER_WRITE)
    doc = dict(engine.to_doc(src), voucher_number=None, is_cancelled=False)
    v = engine.create_voucher(db, company, doc, actor_of(user),
                              reason=f"Duplicated from {src.voucher_type.name} {src.voucher_number}")
    db.commit()
    return _out(db, v)


@router.get("/vouchers/{voucher_id}/versions")
def voucher_versions(voucher_id: uuid.UUID, db: DB, user: CurrentUser):
    v = _load(db, user, voucher_id, Perm.VOUCHER_READ)
    rows = db.scalars(select(VoucherVersion).where(VoucherVersion.voucher_id == v.id).order_by(VoucherVersion.version)).all()
    out, prev = [], None
    for r in rows:
        changes = [] if prev is None else [{"field": d["field"], "before": d["erp"], "after": d["tally"]}
                                           for d in canonical.diff(prev.snapshot, r.snapshot)]
        if prev is not None and prev.status != r.status:
            changes.insert(0, {"field": "status", "before": prev.status, "after": r.status})
        out.append({"version": r.version, "status": r.status, "changed_by": r.changed_by, "source": r.source_system,
                    "reason": r.reason, "at": r.created_at, "hash": r.content_hash, "snapshot": r.snapshot,
                    "changes": changes})
        prev = r
    return out
