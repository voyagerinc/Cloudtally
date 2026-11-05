"""Server tab: only a super admin may ask for an update or restart, and only by dropping a request file."""
import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from tests.test_api import admin, make_user  # noqa: F401  (fixture)

client = TestClient(app)
API = "/api/v1/admin/system"


@pytest.fixture()
def update_dir(tmp_path, monkeypatch):
    (tmp_path / "inbox").mkdir()
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(settings, "update_dir", str(tmp_path))
    return tmp_path


def test_not_set_up_and_not_for_other_users(db, admin, monkeypatch):
    monkeypatch.setattr(settings, "update_dir", None)
    r = client.get(API, headers=admin)
    assert r.status_code == 404 and "not set up" in r.json()["detail"]
    other = make_user(db, "clerk")
    assert client.get(API, headers=other).status_code == 403
    assert client.post(f"{API}/update", headers=other).status_code == 403


def test_request_is_written_once_and_status_is_read(db, admin, update_dir):
    assert client.get(API, headers=admin).json()["state"] == "never"
    assert client.post(f"{API}/reboot", headers=admin).status_code == 422          # fixed actions only

    r = client.post(f"{API}/update", headers=admin)
    assert r.status_code == 202
    assert (update_dir / "inbox" / "request").read_text() == "update"
    assert client.post(f"{API}/restart", headers=admin).status_code == 409         # one at a time
    assert client.get(API, headers=admin).json()["request_pending"]

    # What deploy/update.sh leaves behind after its run.
    (update_dir / "inbox" / "request").unlink()
    (update_dir / "state" / "status.json").write_text(json.dumps({
        "state": "ok", "message": "Updated and restarted", "action": "update", "started_at": "2026-11-05T10:00:00+05:30",
        "finished_at": "2026-11-05T10:01:00+05:30", "commit": "abc1234", "subject": "New users tab",
        "committed_at": "2026-11-05T09:00:00+05:30", "behind": 0, "pending": []}))
    (update_dir / "state" / "update.log").write_text("==> Pulling 1 commit(s)\n==> The app is running\n")
    s = client.get(API, headers=admin).json()
    assert s["state"] == "ok" and s["commit"] == "abc1234" and not s["request_pending"]
    assert "The app is running" in s["log"]
    assert client.post(f"{API}/check", headers=admin).status_code == 202


def test_a_run_that_died_does_not_block_forever(db, admin, update_dir):
    (update_dir / "state" / "status.json").write_text(json.dumps(
        {"state": "running", "action": "update", "started_at": "2026-01-01T00:00:00+00:00"}))
    assert client.get(API, headers=admin).json()["state"] == "failed"
    assert client.post(f"{API}/restart", headers=admin).status_code == 202
