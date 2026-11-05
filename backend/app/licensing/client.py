"""Installation side: activate against the cloud, check in every day, deactivate before moving server.

Only the licence key's first use goes over the wire; the cloud then gives this installation a secret
of its own for later calls. No business data is ever sent (Cloud Licensing PRD §11)."""
import logging
import socket
import threading
import uuid

import httpx
from sqlalchemy.orm import Session

from app import __version__
from app.core.config import settings
from app.core.db import SessionLocal
from app.licensing import enforce
from app.licensing.core import utc, verify
from app.models import LicenseState
from app.models.base import utcnow

log = logging.getLogger(__name__)
CHECK_EVERY_SECONDS = 3600      # wake up hourly; call the cloud when the licence's heartbeat interval is due


class LicenceError(Exception):
    pass


def state(db: Session) -> LicenseState:
    """This installation's row, made at the first start with a new installation id (not hardware based)."""
    st = db.get(LicenseState, 1)
    if st is None:
        st = LicenseState(id=1, installation_id=uuid.uuid4().hex + uuid.uuid4().hex[:8])
        db.add(st)
        db.commit()
    return st


def _post(base_url: str, path: str, body: dict) -> dict:
    try:
        r = httpx.post(base_url.rstrip("/") + "/api/v1" + path, json=body, timeout=30)   # TLS verified
    except httpx.HTTPError as exc:
        raise LicenceError(f"Could not reach the licence service at {base_url}: {exc.__class__.__name__}") from exc
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400:
        raise LicenceError(data.get("detail") or f"The licence service answered {r.status_code}")
    return data


def _store(db: Session, st: LicenseState, data: dict) -> None:
    if verify(data.get("token", ""), settings.license_public_key) is None:
        raise LicenceError("The licence service's answer is not signed with the expected key. "
                           "Check ERP_LICENSE_PUBLIC_KEY on this server.")
    now = utcnow()
    st.token, st.last_check_at, st.last_error = data["token"], now, None
    st.highest_time_seen = max(now, utc(st.highest_time_seen) or now)
    st.update_available = data.get("update")
    if data.get("installation_secret"):
        st.installation_secret = data["installation_secret"]


def activate(db: Session, license_key: str, server_label: str | None, server_url: str | None = None) -> None:
    st = state(db)
    url = server_url or st.server_url or settings.license_server_url
    st.last_attempt_at = utcnow()
    try:
        data = _post(url, "/licenses/activate", {
            "license_key": license_key, "installation_id": st.installation_id, "server_label": server_label,
            "hostname": socket.gethostname()[:200], "product_version": __version__})
        _store(db, st, data)
        st.server_url = url
    except LicenceError as exc:
        st.last_error = str(exc)[:500]
        db.commit()
        raise
    db.commit()
    enforce.forget_cached()


def check(db: Session, kind: str = "heartbeat") -> None:
    st = state(db)
    if not st.installation_secret:
        raise LicenceError("This installation is not activated yet")
    st.last_attempt_at = utcnow()
    st.highest_time_seen = max(utcnow(), utc(st.highest_time_seen) or utcnow())
    try:
        data = _post(st.server_url or settings.license_server_url, f"/licenses/{kind}", {
            "installation_id": st.installation_id, "installation_secret": st.installation_secret,
            "product_version": __version__})
        _store(db, st, data)
    except LicenceError as exc:
        st.last_error = str(exc)[:500]
        if "not activated" in str(exc) or "was blocked" in str(exc) or "was released" in str(exc):
            st.token = st.installation_secret = None      # the cloud says this server no longer holds a slot
        db.commit()
        enforce.forget_cached()
        raise
    db.commit()
    enforce.forget_cached()


def deactivate(db: Session) -> None:
    """Gives the licence's server slot back, e.g. before moving to a new server."""
    st = state(db)
    if st.installation_secret:
        _post(st.server_url or settings.license_server_url, "/licenses/deactivate", {
            "installation_id": st.installation_id, "installation_secret": st.installation_secret,
            "product_version": __version__})
    st.token = st.installation_secret = None
    st.update_available = None
    db.commit()
    enforce.forget_cached()


def due(st: LicenseState) -> bool:
    payload = verify(st.token, settings.license_public_key) if st.token else None
    hours = int((payload or {}).get("heartbeat_hours") or 24)
    last = utc(st.last_check_at)
    tried = utc(st.last_attempt_at)
    now = utcnow()
    if tried and (now - tried).total_seconds() < 15 * 60:
        return False                # do not hammer the cloud after a failure
    return last is None or (now - last).total_seconds() >= hours * 3600


class Heartbeat:
    """Background check-in of an installation (ERP_LICENSE_MODE=client)."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="licence-heartbeat", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with SessionLocal() as db:
                    st = state(db)
                    st.highest_time_seen = max(utcnow(), utc(st.highest_time_seen) or utcnow())
                    db.commit()
                    if st.installation_secret and due(st):
                        check(db)
            except LicenceError as exc:
                log.warning("licence check failed: %s", exc)
            except Exception:
                log.exception("licence check failed")
            self._stop.wait(CHECK_EVERY_SECONDS)


heartbeat = Heartbeat()
