import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app import audit
from app.api.deps import DB, CurrentUser, actor_of, authorize
from app.core.security import Perm, ROLES, allowed_company_ids, hash_key, new_api_key
from app.models import Company, FinancialYear, User, UserCompanyAccess
from app.schemas.admin import (
    AccessIn, CompanyIn, CompanyOut, FinancialYearIn, FinancialYearOut, PeriodLockIn, UserIn,
)
from app.services.accounting import masters
from app.services.accounting.masters import fy_bounds

router = APIRouter(tags=["companies & users"])


@router.post("/companies", response_model=CompanyOut, status_code=201)
def create_company(body: CompanyIn, db: DB, user: CurrentUser):
    if not user.is_superadmin:
        raise HTTPException(403, "Only a super admin can create companies")
    company = masters.create_company(db, body.model_dump(), actor_of(user))
    db.commit()
    return company


@router.get("/companies", response_model=list[CompanyOut])
def list_companies(db: DB, user: CurrentUser):
    ids = allowed_company_ids(db, user)
    stmt = select(Company).order_by(Company.name)
    if ids is not None:
        stmt = stmt.where(Company.id.in_(ids))
    return db.scalars(stmt).all()


@router.get("/companies/{company_id}", response_model=CompanyOut)
def get_company(company_id: uuid.UUID, db: DB, user: CurrentUser):
    return authorize(db, user, company_id, Perm.MASTERS_READ)


@router.patch("/companies/{company_id}", response_model=CompanyOut)
def update_company(company_id: uuid.UUID, body: CompanyIn, db: DB, user: CurrentUser):
    company = authorize(db, user, company_id, Perm.COMPANY_MANAGE)
    before = CompanyOut.model_validate(company).model_dump()
    for k, v in body.model_dump().items():
        setattr(company, k, v)
    audit.record(db, actor_of(user), "company.update", "company", company.id, company_id=company.id,
                 before=before, after=body.model_dump())
    db.commit()
    return company


@router.get("/companies/{company_id}/financial-years", response_model=list[FinancialYearOut])
def list_years(company_id: uuid.UUID, db: DB, user: CurrentUser):
    return authorize(db, user, company_id, Perm.MASTERS_READ).financial_years


@router.post("/companies/{company_id}/financial-years", response_model=FinancialYearOut, status_code=201)
def create_year(company_id: uuid.UUID, body: FinancialYearIn, db: DB, user: CurrentUser):
    company = authorize(db, user, company_id, Perm.COMPANY_MANAGE)
    start, end = fy_bounds(body.start_date)
    fy = FinancialYear(company_id=company.id, start_date=start, end_date=end)
    db.add(fy)
    audit.record(db, actor_of(user), "financial_year.create", "company", company.id, company_id=company.id,
                 after={"start": start, "end": end})
    db.commit()
    return fy


@router.patch("/financial-years/{fy_id}", response_model=FinancialYearOut)
def lock_period(fy_id: uuid.UUID, body: PeriodLockIn, db: DB, user: CurrentUser):
    fy = db.get(FinancialYear, fy_id)
    if fy is None:
        raise HTTPException(404, "Financial year not found")
    authorize(db, user, fy.company_id, Perm.PERIOD_LOCK)
    before = {"is_locked": fy.is_locked, "locked_upto": fy.locked_upto}
    if body.is_locked is not None:
        fy.is_locked = body.is_locked
    if "locked_upto" in body.model_fields_set:
        fy.locked_upto = body.locked_upto
    audit.record(db, actor_of(user), "financial_year.lock", "financial_year", fy.id, company_id=fy.company_id,
                 before=before, after={"is_locked": fy.is_locked, "locked_upto": fy.locked_upto})
    db.commit()
    return fy


# ------------------------------------------------------------------ users

@router.post("/users", status_code=201)
def create_user(body: UserIn, db: DB, user: CurrentUser):
    if not user.is_superadmin:
        raise HTTPException(403, "Only a super admin can create users")
    if db.scalar(select(User.id).where(User.username == body.username)):
        raise HTTPException(409, "Username already exists")
    key = new_api_key()
    new = User(username=body.username, display_name=body.display_name, is_superadmin=body.is_superadmin,
               api_key_hash=hash_key(key))
    db.add(new)
    db.flush()
    audit.record(db, actor_of(user), "user.create", "user", new.id, after=body.model_dump())
    db.commit()
    return {"id": new.id, "username": new.username, "api_key": key, "note": "Shown once; store it securely."}


@router.put("/companies/{company_id}/access")
def grant_access(company_id: uuid.UUID, body: AccessIn, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.USERS_MANAGE)
    if body.role not in ROLES:
        raise HTTPException(422, f"role must be one of {sorted(ROLES)}")
    access = db.get(UserCompanyAccess, (body.user_id, company_id))
    if access is None:
        access = UserCompanyAccess(user_id=body.user_id, company_id=company_id, role=body.role)
        db.add(access)
    access.role = body.role
    audit.record(db, actor_of(user), "user.access", "user", body.user_id, company_id=company_id, after={"role": body.role})
    db.commit()
    return {"user_id": body.user_id, "company_id": company_id, "role": body.role}


@router.delete("/companies/{company_id}/access/{user_id}", status_code=204)
def revoke_access(company_id: uuid.UUID, user_id: uuid.UUID, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.USERS_MANAGE)
    access = db.get(UserCompanyAccess, (user_id, company_id))
    if access:
        db.delete(access)
        audit.record(db, actor_of(user), "user.revoke", "user", user_id, company_id=company_id)
        db.commit()
