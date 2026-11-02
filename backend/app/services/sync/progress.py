"""Live progress of a sync run, for the progress bars on the Sync tab.

Written to its own table on its own short transaction, so viewers see it while the sync's own
transaction is still open and it never waits on rows the sync has locked.
"""
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text

from app.core.db import engine
from app.models.base import utcnow

STEPS = ("Check Tally", "Masters from Tally", "Vouchers from Tally", "ERP changes to Tally")
# A run that has not reported for this long is treated as dead (worker killed, server restarted).
STALE_AFTER = timedelta(minutes=5)


class Progress:
    def __init__(self, company_id: uuid.UUID, trigger: str):
        self.company_id = company_id
        self.data = {"state": "running", "trigger": trigger, "started_at": utcnow().isoformat(), "finished_at": None,
                     "summary": None, "steps": [{"name": n, "state": "pending", "detail": "", "done": None, "total": None}
                                                for n in STEPS]}
        self._write()

    def step(self, index: int, detail: str = "", done: int | None = None, total: int | None = None,
             state: str = "running") -> None:
        for earlier in self.data["steps"][:index]:
            if earlier["state"] in ("pending", "running"):
                earlier["state"] = "done"
        s = self.data["steps"][index]
        s.update(state=state, detail=detail, done=done, total=total)
        self._write()

    def finish(self, summary: str, failed: bool = False) -> None:
        for s in self.data["steps"]:
            if s["state"] == "running":
                s["state"] = "failed" if failed else "done"
            elif s["state"] == "pending":
                s["state"] = "skipped"
        self.data.update(state="failed" if failed else "done", summary=summary, finished_at=utcnow().isoformat())
        self._write()

    def _write(self) -> None:
        self.data["updated_at"] = utcnow().isoformat()
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO sync_progress (company_id, data, updated_at) VALUES (:c, CAST(:p AS jsonb), now()) "
                "ON CONFLICT (company_id) DO UPDATE SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at"),
                {"p": json.dumps(self.data, default=str), "c": self.company_id})


class NoProgress:
    """Stand-in when nobody is watching (tests, CLI imports)."""

    def step(self, *args, **kwargs) -> None:
        pass

    def finish(self, *args, **kwargs) -> None:
        pass


def is_running(progress: dict | None) -> bool:
    if not progress or progress.get("state") != "running":
        return False
    updated = datetime.fromisoformat(progress["updated_at"])
    return utcnow() - updated < STALE_AFTER

