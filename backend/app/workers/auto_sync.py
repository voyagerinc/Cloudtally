"""Automatic sync inside the API server: no separate worker needs to be started.

Every company with an enabled connector is synced in turn, and the next round starts as soon as the
previous one ends: after `sync_backlog_pause` seconds while Tally still has changed vouchers waiting
(one sync slot of up to 1,000 vouchers after another), otherwise after `sync_poll_seconds`. A round
costs Tally almost nothing when nothing changed (it reads two change counters), and nothing at all
when Tally is closed (the connection is refused at once).
"""
import logging
import threading
import time

from sqlalchemy import select

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import SyncProgress, TallyConnector
from app.models.base import utcnow
from app.services.sync import progress
from app.workers.sync_worker import run_company

log = logging.getLogger("sync.auto")


class AutoSync:
    def __init__(self):
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.paused = not settings.auto_sync
        self.last_round_at = None
        self.next_round_at = None
        self.backlog = False

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="auto-sync", daemon=True)
            self._thread.start()
            log.info("automatic sync started")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def set_paused(self, paused: bool) -> None:
        """Switched from the Sync tab; takes effect at once (a running round finishes first)."""
        self.paused = paused
        if not paused:
            self.start()
            self._wake.set()

    def status(self) -> dict:
        return {"enabled": self._thread is not None and not self._stop.is_set() and not self.paused,
                "poll_seconds": settings.sync_poll_seconds,
                "last_round_at": self.last_round_at, "next_round_at": self.next_round_at, "working_through_backlog": self.backlog}

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.paused:
                self._wake.wait(settings.sync_poll_seconds)
                self._wake.clear()
                continue
            try:
                self.backlog = self.run_round()
            except Exception:
                log.exception("automatic sync round failed")
                self.backlog = False
            self.last_round_at = utcnow()
            wait = settings.sync_backlog_pause if self.backlog else settings.sync_poll_seconds
            self.next_round_at = utcnow().timestamp() + wait
            self._wake.wait(wait)
            self._wake.clear()

    @staticmethod
    def run_round() -> bool:
        """Sync every enabled company once. True when some company still has vouchers waiting in Tally."""
        with SessionLocal() as db:
            ids = list(db.scalars(select(TallyConnector.company_id).where(TallyConnector.enabled.is_(True))))
        backlog = False
        for company_id in ids:
            with SessionLocal() as db:
                row = db.get(SyncProgress, company_id)
                if row and progress.is_running(row.data):
                    continue   # a Sync now is already running for this company
            run_company(SessionLocal, company_id, trigger="automatic sync")
            with SessionLocal() as db:
                conn = db.get(TallyConnector, company_id)
                backlog |= bool(conn and conn.voucher_backlog and conn.health_status == "ok")
            time.sleep(0)   # let other threads run between companies
        return backlog


auto_sync = AutoSync()
