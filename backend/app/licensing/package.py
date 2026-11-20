"""The Windows installer package, built by the licence portal from the code it runs.

So customers always find an installer for the portal's version without anybody uploading one: at
start-up (and on the Releases tab's "Build installer now") the portal packs its own code, signs the
package and publishes it, unless that version or a newer one is already released."""
import hashlib
import io
import logging
import os
import uuid
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__, audit
from app.audit import Actor
from app.core.config import settings
from app.licensing.core import public_key_b64, sign_bytes, version_tuple
from app.models import SoftwareRelease
from app.models.base import utcnow

log = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parents[3]

# Never in a package: secrets, environments, build output, caches.
SKIP_DIRS = {".git", ".github", ".venv", "venv", "dist", "updates", "__pycache__", ".pytest_cache", "node_modules",
             "pgdata", "logs", "localdb", "Fixedassets"}
SKIP_FILES = {".env"}
# Office documents and spreadsheets in the folder are the user's business files, never part of the software.
SKIP_SUFFIXES = (".pyc", ".pyo", ".docx", ".doc", ".xlsx", ".xlsm", ".xls", ".csv", ".pdf", ".log", ".pem", ".key",
                 ".zip", ".part")

# Raw strings (Windows paths keep their backslashes); written with Windows line ends.
RUN_BAT = r"""@echo off
rem Tally Cloud. Double-click: the first time it installs Tally Cloud (Windows asks to allow it),
rem later it just starts it and opens the browser. After installing, the "Tally Cloud" icon on the
rem desktop (C:\TallyCloud\start.bat) does the same.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\deploy\windows\run.ps1" %*
"""

README = r"""Tally Cloud {version}

Install and run (Windows server or PC, next to Tally):
  1. Unpack this zip, e.g. to C:\TallyCloud-setup
  2. Double-click run.bat and choose "Yes" when Windows asks to allow it.
     The first time it installs everything (a few minutes) and opens Tally Cloud in the browser:
     enter your licence key and create the manager's user ID and password.
  3. Next time: double-click "Tally Cloud" on the desktop (or C:\TallyCloud\start.bat, or run.bat again).
     Tally Cloud also starts by itself with Windows.
  Other users open  http://<this computer's name>:8080  on the office network.

Updates: the manager opens Licence -> Check for updates -> Install update. Nothing to unpack by hand.
"""


def package_files(root: Path = REPO):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name in SKIP_FILES or name.startswith((".env", "~$")) or name.lower().endswith(SKIP_SUFFIXES):
                continue
            path = Path(dirpath) / name
            yield path, path.relative_to(root).as_posix()


def build(version: str = __version__, root: Path = REPO, public_key: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("VERSION", version + "\n")
        z.writestr("run.bat", RUN_BAT.replace("\n", "\r\n"))
        z.writestr("README.txt", README.format(version=version).replace("\n", "\r\n"))
        if public_key:
            z.writestr("license_public_key.txt", public_key + "\n")
        for path, rel in package_files(root):
            z.write(path, f"app/{rel}")
    return buf.getvalue()


def ensure_current_release(db: Session, *, force: bool = False, actor: Actor | None = None) -> SoftwareRelease | None:
    """Publishes the portal's own version as the Windows installer when no release covers it yet."""
    if settings.license_mode != "server" or not settings.release_dir or not settings.license_signing_key_file:
        return None
    existing = list(db.scalars(select(SoftwareRelease).where(SoftwareRelease.platform == "windows",
                                                             SoftwareRelease.product == "tallycloud")))
    if any(r.version == __version__ for r in existing):
        return None
    if not force and any(version_tuple(r.version) > version_tuple(__version__) for r in existing):
        return None        # a newer release was uploaded (e.g. from GitHub); do not add an older one
    data = build(__version__, public_key=public_key_b64())
    rel = SoftwareRelease(id=uuid.uuid4(), product="tallycloud", version=__version__, platform="windows",
                          file_name=f"TallyCloud-{__version__}.zip", file_size=len(data),
                          sha256=hashlib.sha256(data).hexdigest(), status="published", published_at=utcnow(),
                          release_notes=f"Tally Cloud {__version__}", created_by=(actor.name if actor else "portal"))
    rel.signature = sign_bytes(rel.sha256.encode())
    folder = Path(settings.release_dir)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".build-{rel.id}"
    tmp.write_bytes(data)
    os.replace(tmp, folder / f"{rel.id}-{rel.file_name}")
    db.add(rel)
    audit.record(db, actor or Actor("portal", "system"), "release.build", "release", rel.id,
                 after={"version": rel.version, "sha256": rel.sha256, "size": rel.file_size, "status": rel.status})
    db.commit()
    log.info("published installer %s (%d KB)", rel.file_name, rel.file_size // 1024)
    return rel
