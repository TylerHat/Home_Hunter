"""Offline tests for the POST /rescan trigger and GET /rescan/status poll.

Forces an in-memory SQLite engine before importing the app, then monkeypatches
``clear_all`` and ``pipeline.run`` so the rescan state machine is exercised
without touching the network or a real scrape. Stays fully offline.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from home_hunter.db import UpsertStats


@pytest.fixture
def app_module(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    from home_hunter.api import app as app_module  # import after env is forced

    # Wipe is a no-op here — the state machine, not the DB, is under test.
    monkeypatch.setattr(app_module, "clear_all", lambda session: 7)

    # Reset shared job state so tests don't leak into each other.
    app_module._rescan.clear()
    app_module._rescan.update(app_module._idle_rescan_state())
    yield app_module

    # Make sure no background thread outlives the test.
    thread = app_module._rescan_thread
    if thread is not None:
        thread.join(timeout=5)


def _wait_for(client, predicate, timeout=5.0):
    """Poll /rescan/status until predicate(state) is true; return that state."""
    deadline = time.time() + timeout
    state = client.get("/rescan/status").json()
    while not predicate(state) and time.time() < deadline:
        time.sleep(0.02)
        state = client.get("/rescan/status").json()
    return state


def test_status_is_idle_before_any_run(app_module):
    with TestClient(app_module.app) as client:
        st = client.get("/rescan/status").json()
        assert st["status"] == "idle"
        assert st["progress"] == 0.0


def test_rescan_runs_and_reports_progress(app_module, monkeypatch):
    def fake_run(config, *, only_area=None, on_progress=None):
        assert on_progress is not None
        on_progress({"type": "area_start", "area": "Manhattan", "index": 0, "total": 1})
        on_progress({"type": "summaries", "area": "Manhattan", "count": 3})
        for _ in range(3):
            on_progress({"type": "listing", "area": "Manhattan"})
        on_progress({"type": "area_done", "area": "Manhattan"})
        on_progress({"type": "finalizing"})
        on_progress({"type": "done"})
        return UpsertStats(inserted=3, updated=1, duplicates_merged=0, price_changes=2)

    monkeypatch.setattr(app_module.pipeline, "run", fake_run)

    with TestClient(app_module.app) as client:
        assert client.post("/rescan").json() == {"status": "running"}
        st = _wait_for(client, lambda s: s["status"] in ("done", "error"))

    assert st["status"] == "done"
    assert st["progress"] == 1.0
    assert st["found"] == 3
    assert st["deleted"] == 7
    assert st["stats"]["inserted"] == 3
    assert st["stats"]["updated"] == 1


def test_second_rescan_while_running_returns_409(app_module, monkeypatch):
    gate = threading.Event()

    def blocking_run(config, *, only_area=None, on_progress=None):
        on_progress({"type": "area_start", "area": "Manhattan", "index": 0, "total": 1})
        gate.wait(5)
        on_progress({"type": "done"})
        return UpsertStats(inserted=1)

    monkeypatch.setattr(app_module.pipeline, "run", blocking_run)

    with TestClient(app_module.app) as client:
        assert client.post("/rescan").status_code == 200
        # A second trigger while the first is still running is rejected.
        again = client.post("/rescan")
        assert again.status_code == 409

        gate.set()  # let the first run finish
        st = _wait_for(client, lambda s: s["status"] == "done")
        assert st["status"] == "done"
