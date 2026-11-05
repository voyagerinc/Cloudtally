"""System admin tab: a super admin pulls the latest version from GitHub and restarts the app.

The app has no root rights. It only drops a request file naming a fixed action; deploy/update.sh,
started by systemd as root, does the work and reports back through status.json and update.log."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException

from app import audit
from app.api.deps import DB, CurrentUser, actor_of
from app.core.config import settings

router = APIRouter(prefix="/admin/system", tags=["server"])

STALE_RUN = timedelta(minutes=20)   # a "running" status older than this is from a run that died


def _base(user) -> Path:
    if not user.is_superadmin:
        raise HTTPException(403, "Only a super admin can update or restart the server")
    if not settings.update_dir:
        raise HTTPException(404, "Updating from the web is not set up here. On the server, run deploy/server-setup.sh once more.")
    return Path(settings.update_dir)


def _status(base: Path) -> dict:
    try:
        status = json.loads((base / "state" / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {"state": "never"}
    started = status.get("started_at")
    if status.get("state") == "running" and started:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(started) > STALE_RUN:
                status["state"] = "failed"
                status["message"] = "The last run stopped without finishing. See the log."
        except ValueError:
            pass
    status["request_pending"] = (base / "inbox" / "request").exists()
    return status


@router.get("")
def system_status(user: CurrentUser):
    base = _base(user)
    status = _status(base)
    try:
        lines = (base / "state" / "update.log").read_text(encoding="utf-8", errors="replace").splitlines()
        status["log"] = "\n".join(lines[-200:])
    except OSError:
        status["log"] = ""
    return status


@router.post("/{action}", status_code=202)
def request_action(action: Literal["check", "update", "restart"], db: DB, user: CurrentUser):
    base = _base(user)
    status = _status(base)
    if status["request_pending"] or status.get("state") == "running":
        raise HTTPException(409, "An update or restart is already under way")
    inbox = base / "inbox"
    tmp = inbox / f".request-{os.getpid()}"
    tmp.write_text(action, encoding="utf-8")
    os.replace(tmp, inbox / "request")      # appears whole, so the watcher never reads half a file
    audit.record(db, actor_of(user), f"server.{action}", "server", None)
    db.commit()
    return {"queued": action}
