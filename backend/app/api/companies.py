import uuid

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from sqlalchemy import select

from app import audit
from app.api.deps import DB, CurrentUser, actor_of, authorize
from app.core.security import Perm, ROLES, allowed_company_ids, hash_key, new_api_key
from app.models import Company, FinancialYear, User, UserCompanyAccess
from app.schemas.admin import (
    AccessIn, CompanyIn, CompanyOut, FinancialYearIn, FinancialYearOut, PeriodLockIn, UserIn,
)
from app.licensing import enforce as licence
from app.services.accounting import masters
from app.services.accounting.masters import fy_bounds

router = APIRouter(tags=["companies & users"])


@router.post("/companies", response_model=CompanyOut, status_code=201)
def create_company(body: CompanyIn, db: DB, user: CurrentUser):
    if not user.is_superadmin:
        raise HTTPException(403, "Only a super admin can create companies")
    licence.check_company_limit(db)
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


@router.delete("/companies/{company_id}")
def delete_company(company_id: uuid.UUID, db: DB, user: CurrentUser, confirm: str = ""):
    """Deletes the company and everything in it from this software (Tally is not touched).
    confirm: the company's name, typed by the user."""
    from app.models import SyncProgress
    from app.services.accounting.delete_company import delete_company as wipe
    from app.services.sync import progress
    company = authorize(db, user, company_id, Perm.COMPANY_MANAGE)
    if confirm.strip().casefold() != company.name.strip().casefold():
        raise HTTPException(422, f"Type the company name ({company.name}) to confirm.")
    row = db.get(SyncProgress, company_id)
    if row and progress.is_running(row.data):
        raise HTTPException(409, "A sync is running for this company. Wait until it has finished, then delete.")
    name = company.name
    db.expunge(company)
    rows = wipe(db, company_id)
    audit.record(db, actor_of(user), "company.delete", "company", company_id, company_id=company_id,
                 before={"name": name}, after={"rows_deleted": rows})
    db.commit()
    licence.forget_cached()
    return {"deleted": name, "rows": rows}


# ------------------------------------------------------------------ Company data (Tally connector page)
def _company_admin(db, user, company_id):
    """The company admin (or a super admin): the one who may clear, back up and restore the company's data."""
    company = authorize(db, user, company_id, Perm.COMPANY_MANAGE)
    from app.models import SyncProgress
    from app.services import backup
    from app.services.sync import progress
    row = db.get(SyncProgress, company_id)
    if row and progress.is_running(row.data):
        raise HTTPException(409, "A sync is running for this company. Wait until it has finished.")
    if backup.busy():
        raise HTTPException(409, "A restore is running on this server. Wait until it has finished.")
    return company


@router.post("/companies/{company_id}/reset", status_code=202)
def reset_company(company_id: uuid.UUID, db: DB, user: CurrentUser, background: BackgroundTasks, confirm: str = ""):
    """Deletes the company's data and reads everything again from Tally (masters, then vouchers)."""
    from app.models import TallyConnector
    from app.services.accounting.delete_company import reset_company_data
    company = _company_admin(db, user, company_id)
    if db.get(TallyConnector, company_id) is None:
        raise HTTPException(409, "This company is not linked to Tally, so its data could not be read again.")
    if confirm.strip().casefold() != company.name.strip().casefold():
        raise HTTPException(422, f"Type the company name ({company.name}) to confirm.")
    rows = reset_company_data(db, company_id)
    audit.record(db, actor_of(user), "company.reset", "company", company_id, company_id=company_id,
                 after={"rows_deleted": rows}, reason="data deleted to read it again from Tally")
    db.commit()
    background.add_task(_read_again_from_tally, company_id, user.username, actor_of(user))
    return {"started": True, "rows_deleted": rows, "progress": f"/api/v1/sync/progress?company_id={company_id}"}


def _read_again_from_tally(company_id: uuid.UUID, who: str, actor) -> None:
    from app.api.sync import ImportIn, _run_import, _run_in_background
    try:
        _run_import(company_id, ImportIn(company_id=company_id), actor)      # masters, as when it was linked
    finally:
        _run_in_background(company_id, who)                                  # then every voucher (Sync now)


@router.get("/companies/{company_id}/backup")
def company_backup(company_id: uuid.UUID, db: DB, user: CurrentUser, background: BackgroundTasks):
    """A backup of this company only (its books, users with access, audit trail), to restore it later."""
    from datetime import datetime
    from fastapi.responses import FileResponse
    from app.api.backup import _tmp
    from app.services import backup
    company = authorize(db, user, company_id, Perm.COMPANY_MANAGE)
    path = _tmp(".zip")
    try:
        manifest = backup.write_company_backup(path, [company_id])
    except Exception:
        path.unlink(missing_ok=True)
        raise
    audit.record(db, actor_of(user), "company.backup", "company", company_id, company_id=company_id,
                 after={"rows": sum(manifest["tables"].values()), "size": path.stat().st_size})
    db.commit()
    background.add_task(path.unlink, missing_ok=True)
    label = "".join(ch if ch.isalnum() or ch in " -_" else " " for ch in company.name).strip()[:60].replace(" ", "-")
    return FileResponse(path, media_type="application/zip", filename=f"tallycloud-{label}-{datetime.now():%Y%m%d-%H%M}.zip")


@router.post("/companies/{company_id}/restore", status_code=202)
def company_restore(company_id: uuid.UUID, db: DB, user: CurrentUser, file: UploadFile = File(...),
                    confirm: str = Form("")):
    """Replaces this company's data with a backup of the same company (made here or on another server)."""
    import shutil
    from app.api.backup import _tmp
    from app.core.db import SessionLocal, engine
    from app.services import backup
    company = _company_admin(db, user, company_id)
    if confirm.strip().casefold() != company.name.strip().casefold():
        raise HTTPException(422, f"Type the company name ({company.name}) to confirm.")
    path = _tmp(".zip")
    try:
        with path.open("wb") as out:
            shutil.copyfileobj(file.file, out, 1024 * 1024)
        m = backup.read_manifest(path)
        with engine.connect() as conn:
            backup.check_compatible(m, conn)
        if m.get("scope") != "companies":
            raise backup.BackupError("This is a backup of a whole server. A super admin restores it on the System admin page.")
        held = m["companies"]
        if len(held) != 1 or not (held[0]["id"] == str(company_id)
                                  or held[0]["name"].strip().casefold() == company.name.strip().casefold()):
            raise backup.BackupError(f"This backup is of {', '.join(c['name'] for c in held)}, not of {company.name}.")
    except backup.BackupError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    who, me, admin = user.username, user.id, user.is_superadmin
    new_id = uuid.UUID(held[0]["id"])

    def done(job: dict) -> None:
        with SessionLocal() as s:
            if job.get("state") == "ok" and not admin and s.get(Company, new_id) is not None \
                    and s.get(UserCompanyAccess, (me, new_id)) is None:
                s.add(UserCompanyAccess(user_id=me, company_id=new_id, role="owner"))   # keeps the one who restored it in
            audit.record(s, audit.Actor(who), "company.restore", "company", new_id, company_id=new_id,
                         after={"state": job.get("state"), "backup_of": m.get("created_at"),
                                "message": (job.get("message") or "")[:300]})
            s.commit()

    try:
        backup.start_restore(path, who, done, companies=True, replace=True,
                             extra={"for_company": [str(company_id), str(new_id)], "company_id": str(new_id)})
    except backup.BackupError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(409, str(exc))
    return {"started": True, "company_id": new_id, "backup_of": m.get("created_at")}


@router.get("/companies/{company_id}/restore")
def company_restore_status(company_id: uuid.UUID, user: CurrentUser):
    from app.services import backup
    job = backup.JOB
    if str(company_id) not in (job.get("for_company") or []):
        return {"state": "never"}
    return job


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
