import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, authorize
from app.core.security import Perm
from app.models import AuditEvent

router = APIRouter(tags=["audit"])


@router.get("/audit")
def list_audit(company_id: uuid.UUID, db: DB, user: CurrentUser, entity_type: str | None = None,
               entity_id: str | None = None, actor: str | None = None, action: str | None = None,
               limit: int = 100, before_id: int | None = None):
    authorize(db, user, company_id, Perm.AUDIT_READ)
    stmt = select(AuditEvent).where(AuditEvent.company_id == company_id)
    for col, val in ((AuditEvent.entity_type, entity_type), (AuditEvent.entity_id, entity_id),
                     (AuditEvent.actor, actor)):
        if val:
            stmt = stmt.where(col == val)
    if action:
        stmt = stmt.where(AuditEvent.action.startswith(action))
    if before_id:
        stmt = stmt.where(AuditEvent.id < before_id)
    rows = db.scalars(stmt.order_by(AuditEvent.id.desc()).limit(min(limit, 1000)))
    return [{c.name: getattr(r, c.name) for c in AuditEvent.__table__.columns} for r in rows]
