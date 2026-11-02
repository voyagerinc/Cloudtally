import uuid
from datetime import date
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.audit import Actor
from app.core.db import get_db
from app.core.security import has_perm, role_for, user_for_key
from app.models import Company, User
from app.models.base import utcnow

DB = Annotated[Session, Depends(get_db)]


def current_user(db: DB, x_api_key: Annotated[str | None, Header()] = None) -> User:
    if not x_api_key:
        raise HTTPException(401, "Missing X-API-Key header")
    user = user_for_key(db, x_api_key)
    if user is None:
        raise HTTPException(401, "Invalid API key")
    user.last_login_at = utcnow()
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def actor_of(user: User) -> Actor:
    return Actor(user.username, "integration" if user.username.startswith("svc-") else "user", user.id)


def authorize(db: Session, user: User, company_id: uuid.UUID, perm: str) -> Company:
    """Tenant isolation + RBAC in one place: every company-scoped route goes through here."""
    company = db.get(Company, company_id)
    # Same answer for "missing" and "not yours", so company ids cannot be probed.
    if company is None or role_for(db, user, company_id) is None:
        raise HTTPException(404, "Company not found")
    if not has_perm(db, user, company_id, perm):
        raise HTTPException(403, f"Your role does not allow {perm}")
    return company


def period(from_date: Annotated[date | None, Query(alias="from")] = None,
           to_date: Annotated[date | None, Query(alias="to")] = None) -> tuple[date | None, date | None]:
    return from_date, to_date
