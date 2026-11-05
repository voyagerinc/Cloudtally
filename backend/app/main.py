"""FastAPI application: uvicorn app.main:app --reload"""
import logging
from contextlib import asynccontextmanager
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app import audit
from app.api import audit_log, companies, masters, reports, sync, system, users, vouchers
from app.core.config import settings
from app.core.db import engine
from app.services.accounting.vouchers import VoucherError

log = logging.getLogger("api")
STATIC = Path(__file__).resolve().parent / "static"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Automatic sync runs inside the server while it is up (ERP_AUTO_SYNC, on by default)."""
    from app.workers.auto_sync import auto_sync
    if settings.auto_sync:
        auto_sync.start()   # when off, it can still be switched on from the Sync tab
    yield
    auto_sync.stop()


app = FastAPI(
    lifespan=lifespan,
    title="Tally-Compatible Bidirectional ERP",
    version="0.1.0",
    description="PostgreSQL-based accounting platform with bidirectional TallyPrime synchronization.",
)

API = "/api/v1"
for module in (users, companies, masters, vouchers, reports, sync, audit_log, system):
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
    return FileResponse(STATIC / "index.html")

