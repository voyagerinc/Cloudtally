import json
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import AuditEvent

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


@dataclass(frozen=True)
class Actor:
    name: str
    type: str = "user"   # user | integration | system
    user_id: uuid.UUID | None = None


SYSTEM = Actor("system", "system")
TALLY = Actor("tally-connector", "integration")


def _jsonable(value):
    if value is None:
        return None
    return json.loads(json.dumps(value, default=str))


def record(db: Session, actor: Actor, action: str, entity_type: str, entity_id, *, company_id=None,
           before: dict | None = None, after: dict | None = None, reason: str | None = None) -> None:
    db.add(AuditEvent(
        company_id=company_id, actor=actor.name, actor_type=actor.type, action=action,
        entity_type=entity_type, entity_id=str(entity_id) if entity_id else None,
        before=_jsonable(before), after=_jsonable(after), reason=reason, correlation_id=correlation_id.get(),
    ))
