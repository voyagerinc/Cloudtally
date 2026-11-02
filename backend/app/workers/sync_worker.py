"""Background sync worker: python -m app.workers.sync_worker [--once]

Restart-safe: every unit of work is committed to PostgreSQL before the next one starts,
so a crash loses at most the in-flight event, which is recovered as outcome-unknown.
"""
import argparse
import logging
import signal
import time
import uuid

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.connectors.tally import TallyError
from app.core.config import settings
from app.core.db import SessionLocal
from app.models import Company, TallyConnector
from app.models.base import utcnow
from app.services.sync import common, inbound, outbound
from app.services.sync.progress import Progress

log = logging.getLogger("sync.worker")
_stop = False


def check_tally(db, conn: TallyConnector) -> dict:
    """Contact Tally, list its open companies and match the linked one by name and GUID.

    Updates the connector's health fields. A company with the right name but a different GUID is
    treated as a mismatch, so a same-named company is never synced into these books.
    """
    result = {"server_running": False, "open_companies": [], "matched": False,
              "tally_company_name": conn.tally_company_name}
    client = common.client_for(conn)
    try:
        client.ping()
        result["server_running"] = True
        companies = client.list_companies()
        result["open_companies"] = [c["name"] for c in companies if c["name"]]
        by_name = next((c for c in companies if c["name"] == conn.tally_company_name), None)
        by_guid = next((c for c in companies if conn.tally_company_guid and c["guid"] == conn.tally_company_guid), None)
        if by_name and (not conn.tally_company_guid or by_name["guid"] == conn.tally_company_guid):
            if not conn.tally_company_guid:
                conn.tally_company_guid = by_name["guid"]
            result.update(matched=True, books_from=by_name["books_from"], guid=by_name["guid"])
            conn.health_status, conn.health_message = "ok", f"Connected to '{conn.tally_company_name}'"
        elif by_name:
            conn.health_status = "company_mismatch"
            conn.health_message = (f"A company named '{conn.tally_company_name}' is open in Tally, but it is not the "
                                   "company these books were linked to (different GUID). Sync is blocked.")
        elif by_guid:
            conn.health_status = "company_renamed"
            conn.health_message = (f"The linked company is open in Tally as '{by_guid['name']}'. Update the Tally "
                                   "company name on the Connector tab.")
        else:
            conn.health_status = "company_not_loaded"
            conn.health_message = (f"'{conn.tally_company_name}' is not open in Tally"
                                   + (f" (open: {', '.join(result['open_companies'])})" if result["open_companies"] else
                                      " (no company is open)"))
    except TallyError as exc:
        if result["server_running"] or "did not respond" in exc.message:
            # Tally accepted the connection, so it is open. It answers one request at a time: a slow
            # answer usually means it is serving another request, or a report/dialog is open on screen.
            result["server_running"] = True
            conn.health_status = "busy"
            conn.health_message = (
                "Tally is open but busy: it did not answer in time. Usually it is still answering another "
                "request (a sync from this app, or another program using Tally), or a report or dialog is open "
                "on the Tally screen. Wait a minute and check again; restart TallyPrime only if its own screen "
                "has stopped responding.")
        else:
            conn.health_status = "down"
            conn.health_message = exc.message + "."
    finally:
        conn.health_checked_at = utcnow()
        client.close()
    result.update(status=conn.health_status, message=conn.health_message, checked_at=conn.health_checked_at)
    return result


def check_health(db, conn: TallyConnector) -> bool:
    return check_tally(db, conn)["matched"]


def run_company(session_factory: sessionmaker, company_id: uuid.UUID, force: bool = False,
                trigger: str = "background sync") -> dict:
    stats: dict = {}
    progress = Progress(company_id, trigger)
    try:
        with session_factory() as db:
            conn = db.get(TallyConnector, company_id)
            progress.step(0, f"Contacting Tally at {conn.host}:{conn.port}")
            healthy = check_health(db, conn)
            db.commit()
            stats["health"] = conn.health_status
            if not healthy:   # events stay queued; nothing is discarded
                progress.step(0, conn.health_message or conn.health_status, state="failed")
                progress.finish(f"Tally not ready: {conn.health_message or conn.health_status}", failed=True)
                return stats
            progress.step(0, f"Connected to '{conn.tally_company_name}'", state="done")
            company = db.get(Company, company_id)
            client = common.client_for(conn)
            try:
                stats["inbound"] = inbound.poll(db, company, conn, client, force=force, progress=progress)
            except TallyError as exc:
                db.rollback()
                stats["inbound"] = {"error": exc.code, "message": exc.message}
                log.warning("inbound poll failed for %s: %s", conn.tally_company_name, exc.message)
                progress.step(2, exc.message, state="failed")
            finally:
                client.close()
        stats["outbound"] = outbound.process_batch(session_factory, company_id=company_id, progress=progress)
    except Exception as exc:
        progress.finish(f"Stopped: {type(exc).__name__}: {exc}", failed=True)
        raise
    progress.finish(_summary(stats), failed="error" in (stats.get("inbound") or {}))
    return stats


def _summary(stats: dict) -> str:
    inbound_, outbound_ = stats.get("inbound") or {}, stats.get("outbound") or {}
    if "error" in inbound_:
        received = f"receiving from Tally failed ({inbound_['message']})"
    elif inbound_.get("skipped"):
        received = "receiving from Tally is switched off"
    else:
        received = f"{inbound_.get('received', 0)} voucher change(s) received from Tally"
        if inbound_.get("remaining"):
            received += f", {inbound_['remaining']} more on the next sync"
    sent = f"{outbound_.get('completed', 0)} sent to Tally"
    problems = sum(outbound_.get(k, 0) for k in ("failed", "conflict")) + inbound_.get("failed", 0) + inbound_.get("conflict", 0)
    return f"{received}; {sent}" + (f"; {problems} need attention" if problems else "")


def run_once(session_factory: sessionmaker = SessionLocal) -> dict:
    with session_factory() as db:
        ids = list(db.scalars(select(TallyConnector.company_id).where(TallyConnector.enabled.is_(True))))
    return {str(cid): run_company(session_factory, cid) for cid in ids}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    def _handle(*_):
        global _stop
        _stop = True
        log.info("stopping after current cycle")

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    while True:
        started = time.monotonic()
        try:
            for company, stats in run_once().items():
                log.info("company=%s %s", company, stats)
        except Exception:
            log.exception("sync cycle failed")
        if args.once or _stop:
            break
        time.sleep(max(1.0, settings.sync_poll_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
