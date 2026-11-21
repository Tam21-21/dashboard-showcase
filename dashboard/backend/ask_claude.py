"""Ask Claude: answer a question about the next 60 days of the calendar and propose changes.

The server asks Claude Haiku through the Messages API (#471). Claude may call four tools: propose_event
(add), propose_series (a weekly series), propose_remove and propose_move. A proposal is only
returned to the page; nothing reaches Google until Tamer presses APPLY (DECISIONS #446, #447).
The key is read from the server's environment and is never logged or returned.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import calendar_sync
from .config import ANTHROPIC_API_KEY

MODEL = "claude-haiku-5-5"
MAX_ROUNDS = 4
# Four times the 4000 that cut an answer off on 2026-10-13; Haiku 5.5 allows 128K output tokens
# (platform.claude.com model overview, read 2026-10-18) and thinking shares this budget.
MAX_TOKENS = 16000
TIMEOUT_SECONDS = 180
DAYS_AHEAD = 59  # today plus 59 days is 60 days, the span the calendar cache keeps ahead
URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
PROPOSED = "Shown to Tamer as a proposal; not applied yet"
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

_TIME_DESC = "HH:MM, 24 hours"
_DATE_DESC = "YYYY-MM-DD"
_ALL_DAY_DESC = "true for an all-day event; then start and end are both 00:00"


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("propose_event", "Propose adding one calendar event; it is added only if Tamer presses APPLY", {
        "title": {"type": "string"},
        "date": {"type": "string", "description": _DATE_DESC},
        "start": {"type": "string", "description": _TIME_DESC},
        "end": {"type": "string", "description": _TIME_DESC + ", after start"},
        "all_day": {"type": "boolean", "description": _ALL_DAY_DESC},
    }),
    _tool("propose_series", "Propose a weekly series: the same event on chosen weekdays", {
        "title": {"type": "string"},
        "days": {"type": "array", "items": {"type": "string", "enum": list(WEEKDAYS)},
                 "description": "Weekdays, MO to SU"},
        "start": {"type": "string", "description": _TIME_DESC},
        "end": {"type": "string", "description": _TIME_DESC + ", after start"},
        "first_date": {"type": "string", "description": _DATE_DESC + ", a day that falls on one of days"},
        "until": {"type": "string", "description": _DATE_DESC + " of the last day, or empty for no end"},
    }),
    _tool("propose_remove", "Propose removing events, by the ids in the calendar list; send all ids in one call", {
        "event_ids": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    }),
    _tool("propose_move", "Propose moving one event, by its id in the calendar list, to a new day and time", {
        "event_id": {"type": "string"},
        "date": {"type": "string", "description": _DATE_DESC},
        "start": {"type": "string", "description": _TIME_DESC},
        "end": {"type": "string", "description": _TIME_DESC + ", after start"},
        "all_day": {"type": "boolean", "description": _ALL_DAY_DESC},
    }),
]

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class AskError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def paris_today() -> date:
    return datetime.now(ZoneInfo("Europe/Paris")).date()


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS)

def _event_line(e: dict) -> str:
    if e.get("all_day") == 1 or "T" not in e["start_dt"]:
        return f"- [{e['event_id']}] {e['title']}, {e['start_dt'][:10]}, all day"
    parts = [e["title"], e["start_dt"], e["end_dt"]]
    if e.get("location"):
        parts.append(e["location"])
    return f"- [{e['event_id']}] " + ", ".join(parts)


def _user_message(question: str, today: date) -> tuple[str, dict[str, dict]]:
    """The question with the event lines, and the events by id (the only ids Claude may remove or move)."""
    last = today + timedelta(days=DAYS_AHEAD)
    events = calendar_sync.get_events(today.isoformat(), last.isoformat())
    lines = [_event_line(e) for e in events] or ["No events in the next 60 days"]
    text = f"Question: {question}\n\nCalendar, {today.isoformat()} to {last.isoformat()}:\n" + "\n".join(lines)
    return text, {e["event_id"]: e for e in events}


def _system(today: date) -> str:
    return (
        f"You are Tamer's calendar assistant. Today is {today.isoformat()} in the Europe/Paris time zone. "
        "You see the next 60 days of his calendar; each line starts with the event id in square brackets. "
        "Answer briefly in plain words. To change the calendar, call propose_event (add one event), "
        "propose_series (a weekly series on chosen weekdays), propose_remove (remove events: send every id in one call) "
        "or propose_move (move one event to a new day and time). Only ids in the list may be removed or moved. "
        "Nothing changes until Tamer presses APPLY."
    )


def _real_day(value) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _check_times(inp: dict, all_day: bool) -> tuple[tuple[str, str] | None, str | None]:
    start, end = inp.get("start"), inp.get("end")
    if not isinstance(start, str) or not _TIME.match(start):
        return None, "start must be HH:MM in 24 hours"
    if not isinstance(end, str) or not _TIME.match(end):
        return None, "end must be HH:MM in 24 hours"
    if all_day:
        if (start, end) != ("00:00", "00:00"):
            return None, "start and end must both be 00:00 when all_day is true"
    elif end <= start:
        return None, "end must be after start"
    return (start, end), None


def _check_title(inp: dict) -> tuple[str | None, str | None]:
    title = inp.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, "title must be a non-empty string"
    return title.strip(), None


def _check_day_and_times(inp: dict) -> tuple[tuple[str, bool, str, str] | None, str | None]:
    """The shared part of an add and a move: (date, all_day, start, end) or the error."""
    day = _real_day(inp.get("date"))
    if day is None:
        return None, "date must be a real day as YYYY-MM-DD"
    all_day = inp.get("all_day")
    if not isinstance(all_day, bool):
        return None, "all_day must be true or false"
    times, err = _check_times(inp, all_day)
    if err:
        return None, err
    return (day, all_day, times[0], times[1]), None


def _check_add(inp: dict) -> tuple[dict | None, str | None]:
    title, err = _check_title(inp)
    if err:
        return None, err
    when, err = _check_day_and_times(inp)
    if err:
        return None, err
    day, all_day, start, end = when
    return {"kind": "add", "title": title, "date": day, "start": start, "end": end, "all_day": all_day}, None


def _check_series(inp: dict) -> tuple[dict | None, str | None]:
    title, err = _check_title(inp)
    if err:
        return None, err
    days = inp.get("days")
    if not isinstance(days, list) or not days or any(d not in WEEKDAYS for d in days):
        return None, "days must be a list of MO, TU, WE, TH, FR, SA, SU"
    if len(set(days)) != len(days):
        return None, "days must not repeat a day"
    times, err = _check_times(inp, False)
    if err:
        return None, err
    first = _real_day(inp.get("first_date"))
    if first is None:
        return None, "first_date must be a real day as YYYY-MM-DD"
    if WEEKDAYS[date.fromisoformat(first).weekday()] not in days:
        return None, "first_date must fall on one of the days"
    until = inp.get("until")
    if until != "":
        until = _real_day(until)
        if until is None:
            return None, "until must be a real day as YYYY-MM-DD, or empty for no end"
        if until < first:
            return None, "until must not be before first_date"
    return {"kind": "series", "title": title, "days": days, "start": times[0], "end": times[1],
            "first_date": first, "until": until}, None


def _check_remove(inp: dict, known: dict[str, dict]) -> tuple[dict | None, str | None]:
    ids = inp.get("event_ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
        return None, "event_ids must be a non-empty list of ids from the calendar list"
    if len(set(ids)) != len(ids):
        return None, "event_ids must not repeat an id"
    unknown = [i for i in ids if i not in known]
    if unknown:
        return None, f"event_ids has ids that are not in the calendar list: {', '.join(unknown)}"
    return {"kind": "remove", "event_ids": ids, "titles": [known[i]["title"] for i in ids]}, None


def _check_move(inp: dict, known: dict[str, dict]) -> tuple[dict | None, str | None]:
    event_id = inp.get("event_id")
    if not isinstance(event_id, str) or event_id not in known:
        return None, "event_id is not in the calendar list"
    when, err = _check_day_and_times(inp)
    if err:
        return None, err
    day, all_day, start, end = when
    return {"kind": "move", "event_id": event_id, "title": known[event_id]["title"], "date": day,
            "start": start, "end": end, "all_day": all_day}, None


def _check_call(name: str, inp: dict, known: dict[str, dict]) -> tuple[dict | None, str | None]:
    """Returns (proposal, None) or (None, the field-naming error)."""
    if name == "propose_event":
        return _check_add(inp)
    if name == "propose_series":
        return _check_series(inp)
    if name == "propose_remove":
        return _check_remove(inp, known)
    if name == "propose_move":
        return _check_move(inp, known)
    return None, f"Unknown tool {name}"


def _post(client: httpx.Client, body: dict) -> dict:
    try:
        resp = client.post(
            URL, json=body,
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": API_VERSION},
        )
    except httpx.TimeoutException as exc:
        raise AskError(504, f"Claude did not answer in {TIMEOUT_SECONDS} seconds") from exc
    except httpx.HTTPError as exc:
        raise AskError(502, f"Claude could not be reached: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = resp.text[:200] or "no message"
        raise AskError(502, f"Claude answered {resp.status_code}: {detail}")
    try:
        return resp.json()
    except ValueError as exc:
        raise AskError(502, "Claude sent a reply that is not JSON") from exc


def ask(question: str, today: date, history: list[dict] | None = None) -> dict:
    if not ANTHROPIC_API_KEY:
        raise AskError(503, "ANTHROPIC_API_KEY is not set on the server")
    text, known = _user_message(question, today)
    messages: list[dict] = [{"role": t["role"], "content": t["text"]} for t in history or []]
    messages.append({"role": "user", "content": text})
    proposals: list[dict] = []
    with _client() as client:
        for _ in range(MAX_ROUNDS):
            reply = _post(client, {
                "model": MODEL, "max_tokens": MAX_TOKENS, "system": _system(today),
                "tools": TOOLS, "messages": messages,
            })
            stop = reply.get("stop_reason")
            content = reply.get("content") or []
            if stop == "max_tokens":
                raise AskError(502, "Claude's answer was cut off")
            if stop == "end_turn":
                answer = "\n".join(b["text"] for b in content if b.get("type") == "text").strip()
                if not answer:
                    raise AskError(502, "Claude sent an empty answer")
                return {"answer": answer, "proposals": proposals}
            if stop != "tool_use":
                raise AskError(502, f"Claude stopped without an answer: {stop}")
            results = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                proposal, error = _check_call(block.get("name"), block.get("input") or {}, known)
                if error:
                    results.append({"type": "tool_result", "tool_use_id": block["id"],
                                    "content": error, "is_error": True})
                else:
                    proposals.append(proposal)
                    results.append({"type": "tool_result", "tool_use_id": block["id"], "content": PROPOSED})
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": results})
    raise AskError(502, f"Claude did not finish in {MAX_ROUNDS} rounds")
