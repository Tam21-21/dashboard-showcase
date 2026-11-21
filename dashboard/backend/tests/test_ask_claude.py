"""UI25, UI29a: POST /api/life/ask asks Claude with the next 60 days of the calendar and returns text plus add, series, remove and move proposals."""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import ask_claude, calendar_sync
from dashboard.backend.routes import life

PROPOSED = "Shown to Tamer as a proposal; not applied yet"


def _event(title, start, end, location=None, all_day=0, local_only=0):
    return {
        "event_id": title, "calendar_id": "primary", "title": title, "start_dt": start, "end_dt": end,
        "all_day": all_day, "location": location, "description": None, "color_id": None,
        "html_link": None, "synced_at": "2026-10-12T08:00:00", "local_only": local_only,
    }


def _text(text):
    return {"type": "text", "text": text}


def _call(call_id, name="propose_event", **inp):
    if name == "propose_event":
        inp.setdefault("all_day", False)
    return {"type": "tool_use", "id": call_id, "name": name, "input": inp}


def _reply(content, stop="end_turn"):
    return httpx.Response(200, json={"type": "message", "role": "assistant", "content": content, "stop_reason": stop})


class Rig:
    def __init__(self, monkeypatch, replies):
        self.requests: list[httpx.Request] = []
        self.calls: list[tuple[str, str]] = []
        self.events: list[dict] = []
        self._replies = list(replies)

        def handler(request):
            self.requests.append(request)
            item = self._replies.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(ask_claude, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
        monkeypatch.setattr(ask_claude, "ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(ask_claude, "paris_today", lambda: date(2026, 10, 12))

        def get_events(a, b):
            self.calls.append((a, b))
            return self.events

        monkeypatch.setattr(calendar_sync, "get_events", get_events)

    def body(self, i):
        return json.loads(self.requests[i].content)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(life.router)
    return TestClient(app)


def _ask(client, q="What do I have on Tuesday?", history=None):
    body = {"question": q}
    if history is not None:
        body["history"] = history
    return client.post("/api/life/ask", json=body)


def test_a_first_request_shape(client, monkeypatch):
    rig = Rig(monkeypatch, [_reply([_text("Nothing.")])])
    rig.events = [
        _event("Standup", "2026-10-13T09:00:00", "2026-10-13T09:30:00", location="Room 4"),
        _event("Holiday", "2026-10-14", "2026-10-14", all_day=1),
        _event("Gym", "2026-10-15T18:00:00", "2026-10-15T19:00:00", local_only=1),
    ]
    r = _ask(client)
    assert r.status_code == 200
    assert rig.calls == [("2026-10-12", "2026-12-10")]
    req = rig.requests[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "test-key"
    assert req.headers["anthropic-version"] == "2023-06-01"
    body = rig.body(0)
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 16000
    assert "tool_choice" not in body and "thinking" not in body
    assert [t["name"] for t in body["tools"]] == ["propose_event", "propose_series", "propose_remove", "propose_move"]
    assert all(t["strict"] is True for t in body["tools"])
    assert ask_claude.TIMEOUT_SECONDS == 180
    assert "2026-10-12" in body["system"] and "Europe/Paris" in body["system"]
    msg = body["messages"][0]
    assert msg["role"] == "user"
    text = msg["content"]
    assert "What do I have on Tuesday?" in text
    assert "[Standup] Standup, 2026-10-13T09:00:00, 2026-10-13T09:30:00, Room 4" in text
    assert "Holiday, 2026-10-14, all day" in text
    assert "Gym, 2026-10-15T18:00:00, 2026-10-15T19:00:00" in text


def test_a_no_events_line(client, monkeypatch):
    rig = Rig(monkeypatch, [_reply([_text("Free.")])])
    assert _ask(client).status_code == 200
    assert "No events in the next 60 days" in rig.body(0)["messages"][0]["content"]


@pytest.mark.parametrize("with_thinking", [True, False])
def test_b_tool_round_trip(client, monkeypatch, with_thinking):
    content = []
    if with_thinking:
        content.append({"type": "thinking", "thinking": "hmm", "signature": "sig"})
    content += [
        _text("Two ideas."),
        _call("t1", title="Gym", date="2026-10-14", start="18:00", end="19:00"),
        _call("t2", title="Read", date="2026-10-15", start="20:00", end="21:00"),
    ]
    rig = Rig(monkeypatch, [_reply(content, "tool_use"), _reply([_text("Line one."), _text("Line two.")])])
    r = _ask(client)
    assert r.status_code == 200
    assert r.json() == {
        "answer": "Line one.\nLine two.",
        "proposals": [
            {"kind": "add", "title": "Gym", "date": "2026-10-14", "start": "18:00", "end": "19:00", "all_day": False},
            {"kind": "add", "title": "Read", "date": "2026-10-15", "start": "20:00", "end": "21:00", "all_day": False},
        ],
    }
    second = rig.body(1)["messages"]
    assert second[1] == {"role": "assistant", "content": content}
    assert second[2]["role"] == "user"
    assert second[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": PROPOSED},
        {"type": "tool_result", "tool_use_id": "t2", "content": PROPOSED},
    ]


@pytest.mark.parametrize(
    "bad, field",
    [
        ({"title": "X", "date": "2026-10-14", "start": "19:00", "end": "18:00"}, "end"),
        ({"title": "X", "date": "2026-10-14", "start": "19:00", "end": "19:00"}, "end"),
        ({"title": "X", "date": "14/10/2026", "start": "18:00", "end": "19:00"}, "date"),
    ],
)
def test_c_invalid_proposal_is_refused(client, monkeypatch, bad, field):
    rig = Rig(monkeypatch, [_reply([_call("t1", **bad)], "tool_use"), _reply([_text("Sorry.")])])
    r = _ask(client)
    assert r.status_code == 200
    assert r.json() == {"answer": "Sorry.", "proposals": []}
    result = rig.body(1)["messages"][2]["content"][0]
    assert result["tool_use_id"] == "t1" and result["is_error"] is True
    assert field in result["content"]


def test_d_missing_key(client, monkeypatch):
    rig = Rig(monkeypatch, [])
    monkeypatch.setattr(ask_claude, "ANTHROPIC_API_KEY", "")
    r = _ask(client)
    assert r.status_code == 503
    assert r.json()["detail"] == "ANTHROPIC_API_KEY is not set on the server"
    assert rig.requests == []


@pytest.mark.parametrize("status", [400, 429, 500, 529])
def test_e_upstream_error(client, monkeypatch, status):
    Rig(monkeypatch, [httpx.Response(status, json={"type": "error", "error": {"type": "x", "message": "Overloaded"}})])
    r = _ask(client)
    assert r.status_code == 502
    assert r.json()["detail"] == f"Claude answered {status}: Overloaded"


def test_e_cut_off(client, monkeypatch):
    Rig(monkeypatch, [_reply([_text("Half an ans")], "max_tokens")])
    r = _ask(client)
    assert r.status_code == 502
    assert r.json()["detail"] == "Claude's answer was cut off"


def test_e_four_rounds_at_most(client, monkeypatch):
    def again(i):
        return _reply([_call(f"t{i}", title="X", date="2026-10-14", start="18:00", end="19:00")], "tool_use")

    rig = Rig(monkeypatch, [again(i) for i in range(4)])
    r = _ask(client)
    assert r.status_code == 502
    assert r.json()["detail"] == "Claude did not finish in 4 rounds"
    assert len(rig.requests) == 4


def test_e_timeout(client, monkeypatch):
    Rig(monkeypatch, [httpx.ReadTimeout("slow")])
    r = _ask(client)
    assert r.status_code == 504
    assert r.json()["detail"] == "Claude did not answer in 180 seconds"


def test_e_empty_answer_is_an_error(client, monkeypatch):
    Rig(monkeypatch, [_reply([], "end_turn")])
    r = _ask(client)
    assert r.status_code == 502
    assert r.json()["detail"] == "Claude sent an empty answer"


def test_f_question_checks(client, monkeypatch):
    rig = Rig(monkeypatch, [_reply([_text("ok")])])
    r = _ask(client, "   ")
    assert (r.status_code, r.json()["detail"]) == (422, "Question is empty")
    r = _ask(client, "x" * 2001)
    assert (r.status_code, r.json()["detail"]) == (422, "Question is over 2000 characters")
    assert rig.requests == []
    assert _ask(client, "  hi  ").status_code == 200
    assert "Question: hi\n" in rig.body(0)["messages"][0]["content"]


# ---- UI29a: history, four tools, strict checks ----

def _fixture_events(rig):
    rig.events = [
        _event("Workout A", "2026-10-13T07:00:00", "2026-10-13T08:00:00"),
        _event("Workout B", "2026-10-14T07:00:00", "2026-10-14T08:00:00"),
        _event("Race", "2026-10-20", "2026-10-21", all_day=1),
    ]


def test_b_history_becomes_messages(client, monkeypatch):
    rig = Rig(monkeypatch, [_reply([_text("ok")])])
    history = [{"role": "user", "text": "Hi"}, {"role": "assistant", "text": "Hello"}]
    assert _ask(client, "And now?", history).status_code == 200
    msgs = rig.body(0)["messages"]
    assert len(msgs) == 3
    assert msgs[0] == {"role": "user", "content": "Hi"}
    assert msgs[1] == {"role": "assistant", "content": "Hello"}
    assert msgs[2]["role"] == "user" and "And now?" in msgs[2]["content"]


def _turns(n):
    return [{"role": "user" if i % 2 == 0 else "assistant", "text": f"t{i}"} for i in range(n)]


def test_b_history_checks(client, monkeypatch):
    rig = Rig(monkeypatch, [_reply([_text("ok")])])
    r = _ask(client, "q", _turns(21))
    assert (r.status_code, r.json()["detail"]) == (422, "History is over 20 turns")
    bad_role = [{"role": "user", "text": "a"}, {"role": "system", "text": "b"}]
    r = _ask(client, "q", bad_role)
    assert r.status_code == 422 and "turn 2" in r.json()["detail"]
    starts_assistant = [{"role": "assistant", "text": "a"}, {"role": "user", "text": "b"}]
    r = _ask(client, "q", starts_assistant)
    assert r.status_code == 422 and "turn 1" in r.json()["detail"]
    twice = [{"role": "user", "text": "a"}, {"role": "user", "text": "b"}]
    r = _ask(client, "q", twice)
    assert r.status_code == 422 and "turn 2" in r.json()["detail"]
    empty = [{"role": "user", "text": "a"}, {"role": "assistant", "text": "  "}]
    r = _ask(client, "q", empty)
    assert r.status_code == 422 and "turn 2" in r.json()["detail"]
    ends_user = [{"role": "user", "text": "a"}]
    r = _ask(client, "q", ends_user)
    assert r.status_code == 422 and "turn 1" in r.json()["detail"]
    assert rig.requests == []
    assert _ask(client, "q", _turns(20)).status_code == 200


def test_c_all_four_kinds(client, monkeypatch):
    rig = Rig(monkeypatch, [])
    _fixture_events(rig)
    content = [
        _call("t1", title="Football", date="2026-10-14", start="18:30", end="21:30"),
        _call("t2", "propose_series", title="BU", days=["MO", "TU", "WE", "TH", "FR"], start="08:30", end="17:00",
              first_date="2026-10-13", until="2026-11-30"),
        _call("t3", "propose_remove", event_ids=["Workout A", "Workout B"], reason="Cleared"),
        _call("t4", "propose_move", event_id="Race", date="2026-10-25", start="00:00", end="00:00", all_day=True),
    ]
    rig._replies = [_reply(content, "tool_use"), _reply([_text("Done.")])]
    r = _ask(client)
    assert r.status_code == 200
    assert r.json()["proposals"] == [
        {"kind": "add", "title": "Football", "date": "2026-10-14", "start": "18:30", "end": "21:30", "all_day": False},
        {"kind": "series", "title": "BU", "days": ["MO", "TU", "WE", "TH", "FR"], "start": "08:30", "end": "17:00",
         "first_date": "2026-10-13", "until": "2026-11-30"},
        {"kind": "remove", "event_ids": ["Workout A", "Workout B"], "titles": ["Workout A", "Workout B"]},
        {"kind": "move", "event_id": "Race", "title": "Race", "date": "2026-10-25", "start": "00:00", "end": "00:00",
         "all_day": True},
    ]
    results = rig.body(1)["messages"][2]["content"]
    assert all(x["content"] == PROPOSED and "is_error" not in x for x in results)


def test_c_series_without_end(client, monkeypatch):
    rig = Rig(monkeypatch, [_reply([_call("t1", "propose_series", title="BU", days=["MO"], start="08:30", end="17:00",
                                         first_date="2026-10-12", until="")], "tool_use"), _reply([_text("ok")])])
    r = _ask(client)
    assert r.json()["proposals"][0]["until"] == ""


@pytest.mark.parametrize(
    "name, inp",
    [
        ("propose_remove", {"event_ids": ["Workout A", "nope"], "reason": "x"}),
        ("propose_remove", {"event_ids": [], "reason": "x"}),
        ("propose_move", {"event_id": "nope", "date": "2026-10-25", "start": "09:00", "end": "10:00", "all_day": False}),
        ("propose_series", {"title": "BU", "days": [], "start": "08:30", "end": "17:00", "first_date": "2026-10-12", "until": ""}),
        ("propose_series", {"title": "BU", "days": ["XX"], "start": "08:30", "end": "17:00", "first_date": "2026-10-12", "until": ""}),
        ("propose_series", {"title": "BU", "days": ["MO"], "start": "08:30", "end": "17:00", "first_date": "2026-10-12", "until": "2026-10-05"}),
        ("propose_series", {"title": "BU", "days": ["TU"], "start": "08:30", "end": "17:00", "first_date": "2026-10-12", "until": ""}),
        ("propose_series", {"title": "BU", "days": ["MO"], "start": "17:00", "end": "08:30", "first_date": "2026-10-12", "until": ""}),
        ("propose_move", {"event_id": "Workout A", "date": "2026-10-25", "start": "10:00", "end": "09:00", "all_day": False}),
        ("propose_event", {"title": "X", "date": "2026-10-14", "start": "10:00", "end": "09:00", "all_day": False}),
    ],
)
def test_d_bad_call_refused(client, monkeypatch, name, inp):
    rig = Rig(monkeypatch, [])
    _fixture_events(rig)
    rig._replies = [_reply([_call("t1", name, **inp)], "tool_use"), _reply([_text("Sorry.")])]
    r = _ask(client)
    assert r.status_code == 200
    assert r.json() == {"answer": "Sorry.", "proposals": []}
    result = rig.body(1)["messages"][2]["content"][0]
    assert result["is_error"] is True and result["content"]
