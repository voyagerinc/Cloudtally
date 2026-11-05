import uuid
from datetime import date, timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import Actor
from app.core.config import settings
from app.core.db import get_db
from app.core.security import has_perm, hash_key, role_for, user_for_key
from app.licensing import enforce as licence
from app.models import Company, User, UserSession
from app.models.base import utcnow

DB = Annotated[Session, Depends(get_db)]

# What a user who still has to replace an admin-chosen password may do.
_PASSWORD_CHANGE_PATHS = ("/api/v1/me", "/api/v1/auth/")


def session_for(db: Session, token: str) -> UserSession | None:
    now = utcnow()
    s = db.scalar(select(UserSession).where(UserSession.token_hash == hash_key(token)))
    if s is None:
        return None
    if s.expires_at <= now or s.last_seen_at + timedelta(hours=settings.session_idle_hours) <= now:
        db.delete(s)
        db.commit()
        return None
    if now - s.last_seen_at > timedelta(minutes=1):     # keeps the session (and its licence seat) alive
        if now - s.last_seen_at > timedelta(minutes=settings.seat_idle_minutes):
            # Idle long enough to have given its licence seat away: take a seat again, if one is free.
            user = db.get(User, s.user_id)
            if user is not None:
                try:
                    licence.check_seat(db, user)
                except HTTPException as exc:
                    raise HTTPException(409, f"You were away and your licence seat went to someone else. {exc.detail}")
        s.last_seen_at = now
        db.commit()
    return s


def current_user(request: Request, db: DB, x_api_key: Annotated[str | None, Header()] = None,
                 authorization: Annotated[str | None, Header()] = None) -> User:
    user = None
    if authorization and authorization.lower().startswith("bearer "):
        s = session_for(db, authorization[7:].strip())
        user = db.get(User, s.user_id) if s else None
        if user is not None and user.must_change_password and not request.url.path.startswith(_PASSWORD_CHANGE_PATHS):
            raise HTTPException(403, "Change your password first")
    elif x_api_key:
        user = user_for_key(db, x_api_key)
        if user is not None:
            user.last_login_at = utcnow()
    else:
        raise HTTPException(401, "Please sign in")
    if user is None or not user.is_active:
        raise HTTPException(401, "Your sign-in has expired; please sign in again")
    request.state.user = user
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
        raise HTTPException(403, "You do not have permission for this. Ask your company admin.")
    licence.check_perm(db, company_id, perm)     # licensed modules, read-only after expiry
    return company


def period(from_date: Annotated[date | None, Query(alias="from")] = None,
           to_date: Annotated[date | None, Query(alias="to")] = None) -> tuple[date | None, date | None]:
    return from_date, to_date
