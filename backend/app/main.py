"""FastAPI application: uvicorn app.main:app --reload"""
import logging
import threading
from contextlib import asynccontextmanager
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app import __version__, audit
from app.api import (
    audit_log, backup, companies, export, fixed_assets, licence, licensing, masters, mis, reports, sync, system, users, vouchers,
)
from app.core.config import settings
from app.core.db import engine
from app.services.accounting.vouchers import VoucherError

log = logging.getLogger("api")
STATIC = Path(__file__).resolve().parent / "static"

def _publish_own_installer() -> None:
    """Customers always find an installer for the version this portal runs."""
    from app.core.db import SessionLocal
    from app.licensing.package import ensure_current_release
    try:
        with SessionLocal() as db:
            ensure_current_release(db)
    except Exception:
        log.exception("could not build the installer package")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Automatic sync runs inside the server while it is up (ERP_AUTO_SYNC, on by default)."""
    from app.licensing.client import heartbeat
    from app.licensing.service import expiry_notices
    from app.workers.auto_sync import auto_sync
    if settings.auto_sync:
        auto_sync.start()   # when off, it can still be switched on from the Sync tab
    if settings.license_mode == "client":
        heartbeat.start()   # daily licence check-in with the cloud
    elif settings.license_mode == "server":
        expiry_notices.start()
        threading.Thread(target=_publish_own_installer, name="installer", daemon=True).start()
    yield
    auto_sync.stop()
    heartbeat.stop()
    expiry_notices.stop()


app = FastAPI(
    lifespan=lifespan,
    title="Tally-Compatible Bidirectional ERP",
    version=__version__,
    description="PostgreSQL-based accounting platform with bidirectional TallyPrime synchronization.",
)

API = "/api/v1"
for module in (users, companies, backup, masters, vouchers, reports, mis, export, fixed_assets, sync, audit_log, system, licence,
               licensing):
    app.include_router(module.router, prefix=API)


@app.middleware("http")
async def correlation(request: Request, call_next):
    cid = request.headers.get("X-Correlation-ID") or uuid.uuid4().hex
    token = audit.correlation_id.set(cid)
    try:
        response = await call_next(request)
    finally:
        audit.correlation_id.reset(token)
    response.headers["X-Correlation-ID"] = cid
    return response


@app.exception_handler(VoucherError)
async def voucher_error(request: Request, exc: VoucherError):
    return JSONResponse(status_code=404 if exc.code == "not_found" else exc.status_code,
                        content={"code": exc.code, "detail": exc.message})


@app.get("/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("select 1"))
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    """The page, always fresh, with its script and styles tagged by their content: whenever they change (an
    update, or a change between releases) the browser loads the new ones instead of an old copy it kept."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace('/static/app.js"', f'/static/app.js?v={_tag("app.js")}"').replace(
        '/static/app.css"', f'/static/app.css?v={_tag("app.css")}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def _tag(name: str) -> str:
    """Version plus a short fingerprint of the file's content."""
    import hashlib
    path = STATIC / name
    try:
        stat = path.stat()
        key = (name, stat.st_mtime_ns, stat.st_size)
        if _tags.get(name, (None,))[0] != key:
            _tags[name] = (key, hashlib.sha1(path.read_bytes()).hexdigest()[:10])
        return f"{__version__}-{_tags[name][1]}"
    except OSError:
        return __version__


_tags: dict[str, tuple] = {}


@app.get("/fixed-assets", include_in_schema=False)
def fixed_assets_page():
    """Fixed assets & depreciation (signed in with Tally Cloud's session; data from /api/v1/fixed-assets)."""
    return HTMLResponse((STATIC / "fixed-assets.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})

