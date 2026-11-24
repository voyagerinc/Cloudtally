"""System admin > Backup & restore: move Tally Cloud to another server without reading everything from Tally again.

Old server: Download backup. New server (installed and updated to the same version or later): Restore backup."""
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import uuid

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from app import audit
from app.api.deps import DB, CurrentUser
from app.core.db import SessionLocal, engine
from app.models import SyncProgress
from app.services import backup
from app.services.sync import progress

router = APIRouter(prefix="/admin/backup", tags=["server"])


def _superadmin(user) -> None:
    if not user.is_superadmin:
        raise HTTPException(403, "Only a super admin can back up or restore the server")


def _tmp(suffix: str) -> Path:
    f = tempfile.NamedTemporaryFile(prefix="tallycloud-", suffix=suffix, delete=False)
    f.close()
    return Path(f.name)


@router.get("/download")
def download(db: DB, user: CurrentUser, background: BackgroundTasks, company: list[uuid.UUID] = Query(default=[])):
    """The whole database as one .zip (made from one consistent snapshot; work can go on meanwhile).
    company=...: only these companies (repeat for several), to import on another server."""
    _superadmin(user)
    if backup.busy():
        raise HTTPException(409, "A restore is running")
    path = _tmp(".zip")
    try:
        manifest = backup.write_company_backup(path, company) if company else backup.write_backup(path)
    except backup.BackupError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    audit.record(db, audit.Actor(user.username), "server.backup", "server", None,
                 after={"rows": sum(manifest["tables"].values()), "size": path.stat().st_size,
                        "companies": [c["name"] for c in manifest.get("companies", [])] or "all"})
    db.commit()
    background.add_task(path.unlink, missing_ok=True)
    if company:
        label = manifest["companies"][0]["name"] if len(company) == 1 else f"{len(company)}-companies"
        label = "".join(ch if ch.isalnum() or ch in " -_" else " " for ch in label).strip()[:60].replace(" ", "-")
        name = f"tallycloud-{label}-{datetime.now():%Y%m%d-%H%M}.zip"
    else:
        name = f"tallycloud-backup-{datetime.now():%Y%m%d-%H%M}.zip"
    return FileResponse(path, media_type="application/zip", filename=name)


@router.post("/inspect")
def inspect_backup(user: CurrentUser, file: UploadFile = File(...)):
    """What a backup file holds (whole server or which companies), and which of its companies are here already."""
    _superadmin(user)
    path = _tmp(".zip")
    try:
        with path.open("wb") as out:
            shutil.copyfileobj(file.file, out, 1024 * 1024)
        m = backup.read_manifest(path)
    except backup.BackupError as exc:
        raise HTTPException(422, str(exc))
    finally:
        path.unlink(missing_ok=True)
    return {"scope": m.get("scope", "server"), "companies": m.get("companies", []), "created_at": m.get("created_at"),
            "app_version": m.get("app_version"), "existing": backup.existing_companies(m)}


@router.get("/restore")
def restore_status(user: CurrentUser):
    _superadmin(user)
    return backup.JOB


@router.post("/restore", status_code=202)
def restore(db: DB, user: CurrentUser, file: UploadFile = File(...), confirm: str = Form(""),
            replace: bool = Form(False)):
    """A whole-server backup replaces ALL data on this server; a company backup adds its companies (replacing
    the same companies here only with replace). Runs in the background; follow GET /restore."""
    _superadmin(user)
    if backup.busy():
        raise HTTPException(409, "A restore is already running")
    running = [r for r in db.scalars(select(SyncProgress)) if progress.is_running(r.data)]
    if running:
        raise HTTPException(409, "A sync is running. Wait until it has finished, then restore.")
    path = _tmp(".zip")
    try:
        with path.open("wb") as out:
            shutil.copyfileobj(file.file, out, 1024 * 1024)
        manifest = backup.read_manifest(path)
        with engine.connect() as conn:
            backup.check_compatible(manifest, conn)
        companies = manifest.get("scope") == "companies"
        word = "IMPORT" if companies else "RESTORE"
        if confirm.strip().upper() != word:
            raise backup.BackupError(f"Type {word} to confirm."
                                     + ("" if companies else " Every company, voucher and user on this server is replaced."))
    except backup.BackupError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    who = user.username
    db.close()                      # the restore replaces this session's rows too

    from app.workers.auto_sync import auto_sync
    was_paused = auto_sync.paused
    auto_sync.paused = True         # no sync writes while the data is being replaced

    def done(job: dict) -> None:
        auto_sync.paused = was_paused
        with SessionLocal() as s:   # recorded after the restore, so it is kept
            audit.record(s, audit.Actor(who), "server.restore", "server", None,
                         after={"state": job.get("state"), "backup_of": manifest.get("created_at"),
                                "backup_version": manifest.get("app_version"),
                                "message": (job.get("message") or "")[:300]})
            s.commit()

    try:
        backup.start_restore(path, who, done, companies=companies, replace=replace)
    except backup.BackupError as exc:
        auto_sync.paused = was_paused
        path.unlink(missing_ok=True)
        raise HTTPException(409, str(exc))
    return {"started": True, "backup": {k: manifest.get(k) for k in ("created_at", "app_version", "tables", "scope", "companies")}}
