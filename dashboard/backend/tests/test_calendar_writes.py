"""UI29a: calendar writes call Google first, show Google's errors, and change the cache only after Google answers."""
from __future__ import annotations

import json
import sqlite3

import httplib2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

from dashboard.backend import calendar_sync, userdb
from dashboard.backend.deps import get_user_conn
from dashboard.backend.routes import life


def _http_error(status, message):
    resp = httplib2.Response({"status": status, "reason": "x"})
    return HttpError(resp, json.dumps({"error": {"message": message}}).encode())


class FakeService:
    """Stands in for the object `build("calendar", "v3", ...)` returns."""

    def __init__(self):
        self.fail = None
        self.calls: list[tuple] = []

    def events(self):
        return self

    def _run(self, name, **kw):
        outer = self

        class Call:
            def execute(self_inner):
                outer.calls.append((name, kw))
                if outer.fail:
                    raise outer.fail
                if name == "insert":
                    return {"id": "g_new", "summary": kw["body"]["summary"], "start": kw["body"]["start"],
                            "end": kw["body"]["end"]}
                if name == "get":
                    return {"id": kw["eventId"], "summary": "x", "start": {}, "end": {}}
                return {}
        return Call()

    def insert(self, **kw):
        return self._run("insert", **kw)

    def delete(self, **kw):
        return self._run("delete", **kw)

    def get(self, **kw):
        return self._run("get", **kw)

    def update(self, **kw):
        return self._run("update", **kw)


class Rig:
    pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    db = tmp_path / "user.db"
    userdb.init_db(db)
    monkeypatch.setattr(calendar_sync, "open_rw", lambda path=db: userdb.open_rw(path))
    service = FakeService()
    state = {"creds": object(), "synced": 0}
    monkeypatch.setattr(calendar_sync, "_get_credentials", lambda: state["creds"])
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: service)
    monkeypatch.setattr(calendar_sync, "is_connected", lambda: state["creds"] is not None)

    def fake_sync():
        state["synced"] += 1
        return 0

    monkeypatch.setattr(calendar_sync, "sync_events", fake_sync)
    app = FastAPI()
    app.include_router(life.router)

    def conn_dep():
        conn = userdb.open_rw(db)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_user_conn] = conn_dep

    r = Rig()
    r.client = TestClient(app)
    r.service = service
    r.state = state

    def put(event_id, start, end, all_day=0, local_only=0):
        c = sqlite3.connect(db)
        c.execute(
            "INSERT INTO calendar_events (event_id, title, start_dt, end_dt, all_day, local_only) VALUES (?,?,?,?,?,?)",
            (event_id, "T", start, end, all_day, local_only),
        )
        c.commit()
        c.close()

    def row(event_id):
        c = sqlite3.connect(db)
        out = c.execute("SELECT start_dt, end_dt FROM calendar_events WHERE event_id = ?", (event_id,)).fetchone()
        c.close()
        return out

    r.put, r.row = put, row
    return r


RULE = "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
NEW = {"title": "BU", "start_dt": "2026-10-13T08:30:00", "end_dt": "2026-10-13T17:00:00"}


def test_f_create_with_recurrence(rig):
    r = rig.client.post("/api/life/calendar/events", json={**NEW, "recurrence": [RULE]})
    assert r.status_code == 200
    name, kw = rig.service.calls[0]
    assert name == "insert" and kw["body"]["recurrence"] == [RULE]
    assert rig.state["synced"] == 1


def test_f_create_without_recurrence_does_not_sync(rig):
    r = rig.client.post("/api/life/calendar/events", json=NEW)
    assert r.status_code == 200
    assert "recurrence" not in rig.service.calls[0][1]["body"]
    assert rig.state["synced"] == 0


@pytest.mark.parametrize("bad", [[], [RULE, RULE], ["FREQ=WEEKLY"], ["RRULE:FREQ=DAILY"], ["RRULE:FREQ=WEEKLY;BYDAY=XX"]])
def test_f_bad_recurrence(rig, bad):
    r = rig.client.post("/api/life/calendar/events", json={**NEW, "recurrence": bad})
    assert r.status_code == 422
    assert rig.service.calls == []


def test_f_series_sync_refused_after_insert_is_success_with_sync_error(rig, monkeypatch):
    def refuse():
        raise _http_error(500, "Sync down")

    monkeypatch.setattr(calendar_sync, "sync_events", refuse)
    r = rig.client.post("/api/life/calendar/events", json={**NEW, "recurrence": [RULE]})
    assert r.status_code == 200
    body = r.json()
    assert body["event_id"] == "g_new" and body["sync_error"] == "Sync down"
    assert [c[0] for c in rig.service.calls] == ["insert"]


def test_f_sync_error_is_null_when_sync_worked(rig):
    r = rig.client.post("/api/life/calendar/events", json={**NEW, "recurrence": [RULE]})
    assert r.json()["sync_error"] is None


def test_f_create_google_refuses(rig):
    rig.service.fail = _http_error(403, "Quota hit")
    r = rig.client.post("/api/life/calendar/events", json=NEW)
    assert (r.status_code, r.json()["detail"]) == (502, "Google refused the create: Quota hit")


def test_g_delete_google_refuses_keeps_row(rig):
    rig.put("g1", "2026-10-13T07:00:00", "2026-10-13T08:00:00")
    rig.service.fail = _http_error(500, "Backend down")
    r = rig.client.delete("/api/life/calendar/events/g1")
    assert (r.status_code, r.json()["detail"]) == (502, "Google refused the delete: Backend down")
    assert rig.row("g1") is not None


def test_g_delete_not_connected_keeps_row(rig):
    rig.put("g1", "2026-10-13T07:00:00", "2026-10-13T08:00:00")
    rig.state["creds"] = None
    r = rig.client.delete("/api/life/calendar/events/g1")
    assert (r.status_code, r.json()["detail"]) == (503, "Google Calendar not connected")
    assert rig.row("g1") is not None


def test_g_delete_unknown_id(rig):
    r = rig.client.delete("/api/life/calendar/events/nope")
    assert r.status_code == 404
    assert rig.service.calls == []


def test_g_delete_local_row_makes_no_google_call(rig):
    rig.put("local_1", "2026-10-13T07:00:00", "2026-10-13T08:00:00", local_only=1)
    rig.state["creds"] = None
    assert rig.client.delete("/api/life/calendar/events/local_1").status_code == 200
    assert rig.service.calls == [] and rig.row("local_1") is None


def test_g_delete_success(rig):
    rig.put("g1", "2026-10-13T07:00:00", "2026-10-13T08:00:00")
    assert rig.client.delete("/api/life/calendar/events/g1").status_code == 200
    assert rig.service.calls[0][0] == "delete" and rig.service.calls[0][1]["eventId"] == "g1"
    assert rig.row("g1") is None


MOVE = {"start_dt": "2026-10-14T09:00:00", "end_dt": "2026-10-14T10:00:00"}


def test_h_move_google_refuses_keeps_row(rig):
    rig.put("g1", "2026-10-13T07:00:00", "2026-10-13T08:00:00")
    rig.service.fail = _http_error(400, "Bad time")
    r = rig.client.patch("/api/life/calendar/events/g1/move", json=MOVE)
    assert (r.status_code, r.json()["detail"]) == (502, "Google refused the move: Bad time")
    assert rig.row("g1") == ("2026-10-13T07:00:00", "2026-10-13T08:00:00")


def test_h_move_not_connected_keeps_row(rig):
    rig.put("g1", "2026-10-13T07:00:00", "2026-10-13T08:00:00")
    rig.state["creds"] = None
    r = rig.client.patch("/api/life/calendar/events/g1/move", json=MOVE)
    assert (r.status_code, r.json()["detail"]) == (503, "Google Calendar not connected")
    assert rig.row("g1") == ("2026-10-13T07:00:00", "2026-10-13T08:00:00")


def test_h_move_unknown_id(rig):
    assert rig.client.patch("/api/life/calendar/events/nope/move", json=MOVE).status_code == 404


def test_h_move_timed_success(rig):
    rig.put("g1", "2026-10-13T07:00:00", "2026-10-13T08:00:00")
    assert rig.client.patch("/api/life/calendar/events/g1/move", json=MOVE).status_code == 200
    assert rig.row("g1") == (MOVE["start_dt"], MOVE["end_dt"])
    sent = rig.service.calls[-1][1]["body"]
    assert sent["start"]["dateTime"] == MOVE["start_dt"]


def test_h_move_all_day_sends_dates(rig):
    rig.put("g1", "2026-10-20", "2026-10-21", all_day=1)
    body = {"start_dt": "2026-10-25T00:00:00", "end_dt": "2026-10-26T00:00:00"}
    assert rig.client.patch("/api/life/calendar/events/g1/move", json=body).status_code == 200
    sent = rig.service.calls[-1][1]["body"]
    assert sent["start"] == {"date": "2026-10-25"} and sent["end"] == {"date": "2026-10-26"}
    assert rig.row("g1") == ("2026-10-25", "2026-10-26")


def test_h_move_two_day_all_day_keeps_both_dates(rig):
    rig.put("g1", "2026-10-20", "2026-10-22", all_day=1)
    body = {"start_dt": "2026-10-25", "end_dt": "2026-10-27"}
    assert rig.client.patch("/api/life/calendar/events/g1/move", json=body).status_code == 200
    sent = rig.service.calls[-1][1]["body"]
    assert sent["start"] == {"date": "2026-10-25"} and sent["end"] == {"date": "2026-10-27"}
    assert rig.row("g1") == ("2026-10-25", "2026-10-27")
