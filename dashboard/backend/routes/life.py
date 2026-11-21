"""Life-dashboard CRUD — expenses, budget config, todos, Garmin, news."""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
import xml.etree.ElementTree as ET_XML
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from fastapi import Query as QParam

from ..deps import get_user_conn
from ..sse import broadcaster
from .. import garmin as garmin_mod
from .. import ask_claude
from .. import calendar_sync
from .. import evening_summary
from .. import push as push_mod
from .. import weather as weather_mod

router = APIRouter(prefix="/api/life", tags=["life"])


def _row(r: sqlite3.Row) -> dict:
    return dict(r)


async def _pub(event: str, payload: dict) -> None:
    await broadcaster.publish(event, payload)


# ── Expenses ───────────────────────────────────────────────────────────────────

class ExpenseCreate(BaseModel):
    date: str
    label: str
    amount: float = Field(gt=0)
    category: str = "other"


@router.get("/expenses")
def list_expenses(date: str, conn: sqlite3.Connection = Depends(get_user_conn)):
    rows = conn.execute(
        "SELECT * FROM expenses WHERE date = ? ORDER BY created_at",
        (date,),
    ).fetchall()
    return [_row(r) for r in rows]


@router.get("/expenses/monthly-total")
def monthly_expense_total(month: str, conn: sqlite3.Connection = Depends(get_user_conn)):
    """Return total spent in a given month. month = 'YYYY-MM'."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE substr(date, 1, 7) = ?",
        (month,),
    ).fetchone()
    return {"total": row["total"]}


@router.post("/expenses")
def add_expense(
    payload: ExpenseCreate,
    background_tasks: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_user_conn),
):
    cur = conn.execute(
        "INSERT INTO expenses (date, label, amount, category) VALUES (?, ?, ?, ?)",
        (payload.date, payload.label, payload.amount, payload.category),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM expenses WHERE id = ?", (cur.lastrowid,)).fetchone()
    expense = _row(row)
    background_tasks.add_task(_pub, "expense_added", {"expense": expense, "date": payload.date})
    return expense


@router.delete("/expenses/{expense_id}")
def delete_expense(
    expense_id: int,
    background_tasks: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_user_conn),
):
    row = conn.execute("SELECT date FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Expense not found")
    date = row["date"]
    conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    conn.commit()
    background_tasks.add_task(_pub, "expense_deleted", {"id": expense_id, "date": date})
    return {"ok": True}


# ── Budget config ──────────────────────────────────────────────────────────────

class BudgetConfigUpdate(BaseModel):
    monthly_budget: float = Field(gt=0)
    fixed_expenses: float = Field(ge=0)
    # `null` clears the override (return to summing expenses);
    # a number sets a manual total-spent override.
    manual_monthly_spent: Optional[float] = Field(default=None, ge=0)


@router.get("/budget-config")
def get_budget_config(conn: sqlite3.Connection = Depends(get_user_conn)):
    row = conn.execute("SELECT * FROM budget_config WHERE id = 1").fetchone()
    return _row(row)


@router.put("/budget-config")
def update_budget_config(
    payload: BudgetConfigUpdate,
    background_tasks: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_user_conn),
):
    conn.execute(
        """INSERT INTO budget_config (id, monthly_budget, fixed_expenses,
                                       manual_monthly_spent, updated_at)
           VALUES (1, ?, ?, ?, datetime('now'))
           ON CONFLICT (id) DO UPDATE SET
               monthly_budget       = excluded.monthly_budget,
               fixed_expenses       = excluded.fixed_expenses,
               manual_monthly_spent = excluded.manual_monthly_spent,
               updated_at           = excluded.updated_at""",
        (payload.monthly_budget, payload.fixed_expenses, payload.manual_monthly_spent),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM budget_config WHERE id = 1").fetchone()
    config = _row(row)
    background_tasks.add_task(_pub, "budget_updated", {"config": config})
    return config


# ── Todos ──────────────────────────────────────────────────────────────────────

def _check_todo_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("The date must be written YYYY-MM-DD, for example 2026-11-02.")
    if len(value) != 10:
        raise ValueError("The date must be written YYYY-MM-DD, for example 2026-11-02.")
    return value


def _check_todo_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("The to-do needs some text.")
    if len(value) > 200:
        raise ValueError("The to-do can be at most 200 characters long.")
    return value


class TodoCreate(BaseModel):
    date: str
    text: str


class TodoPatch(BaseModel):
    text: Optional[str] = None
    done: Optional[bool] = None
    position: Optional[int] = None
    date: Optional[str] = None


def _refuse(check, value):
    """Run a check; a refusal answers 422 with one plain sentence as the detail (the page shows it as is)."""
    try:
        return check(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class TodoReorder(BaseModel):
    ordered_ids: list[int]


@router.get("/todos")
def list_todos(date: str, conn: sqlite3.Connection = Depends(get_user_conn)):
    rows = conn.execute(
        "SELECT * FROM todos WHERE date = ? ORDER BY position, id",
        (date,),
    ).fetchall()
    return [_row(r) for r in rows]


@router.get("/todos/open")
def list_open_todos(before: str, conn: sqlite3.Connection = Depends(get_user_conn)):
    _refuse(_check_todo_date, before)
    rows = conn.execute(
        "SELECT * FROM todos WHERE done = 0 AND date < ? ORDER BY date, position, id",
        (before,),
    ).fetchall()
    return [_row(r) for r in rows]


@router.get("/todos/done-late")
def list_done_late_todos(day: str, conn: sqlite3.Connection = Depends(get_user_conn)):
    """Items dated before `day` that were ticked during the Paris day `day`."""
    _refuse(_check_todo_date, day)
    paris = ZoneInfo("Europe/Paris")
    first = date.fromisoformat(day)
    fmt = "%Y-%m-%d %H:%M:%S"
    start = datetime.combine(first, datetime.min.time(), tzinfo=paris).astimezone(timezone.utc)
    end = datetime.combine(first + timedelta(days=1), datetime.min.time(), tzinfo=paris).astimezone(timezone.utc)
    rows = conn.execute(
        "SELECT * FROM todos WHERE done = 1 AND date < ? AND done_at >= ? AND done_at < ? "
        "ORDER BY done_at, id",
        (day, start.strftime(fmt), end.strftime(fmt)),
    ).fetchall()
    return [_row(r) for r in rows]


@router.post("/todos")
def create_todo(
    payload: TodoCreate,
    background_tasks: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_user_conn),
):
    day = _refuse(_check_todo_date, payload.date)
    text = _refuse(_check_todo_text, payload.text)
    max_pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) FROM todos WHERE date = ?",
        (day,),
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO todos (text, done, position, date) VALUES (?, 0, ?, ?)",
        (text, max_pos + 1, day),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (cur.lastrowid,)).fetchone()
    todo = _row(row)
    background_tasks.add_task(_pub, "todo_added", {"todo": todo, "date": payload.date})
    return todo


@router.patch("/todos/{todo_id}")
def patch_todo(
    todo_id: int,
    payload: TodoPatch,
    background_tasks: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_user_conn),
):
    current = conn.execute("SELECT done FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if not current:
        raise HTTPException(status_code=404, detail="Todo not found")

    updates: dict = {}
    if payload.text is not None:
        updates["text"] = _refuse(_check_todo_text, payload.text)
    if payload.done is not None:
        updates["done"] = int(payload.done)
        if payload.done and not current["done"]:
            updates["done_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        elif not payload.done:
            updates["done_at"] = None
    if payload.position is not None:
        updates["position"] = payload.position
    if payload.date is not None:
        updates["date"] = _refuse(_check_todo_date, payload.date)

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE todos SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            [*updates.values(), todo_id],
        )
        conn.commit()

    todo = _row(conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone())
    event = "todo_toggled" if payload.done is not None else "todo_updated"
    background_tasks.add_task(_pub, event, {"todo": todo, "date": todo["date"]})
    return todo


@router.delete("/todos/{todo_id}")
def delete_todo(
    todo_id: int,
    background_tasks: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_user_conn),
):
    row = conn.execute("SELECT date FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Todo not found")
    date = row["date"]
    conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    conn.commit()
    background_tasks.add_task(_pub, "todo_deleted", {"id": todo_id, "date": date})
    return {"ok": True}


@router.post("/todos/reorder")
def reorder_todos(
    payload: TodoReorder,
    background_tasks: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_user_conn),
):
    for i, todo_id in enumerate(payload.ordered_ids):
        conn.execute(
            "UPDATE todos SET position = ?, updated_at = datetime('now') WHERE id = ?",
            (i, todo_id),
        )
    conn.commit()
    background_tasks.add_task(_pub, "todos_reordered", {"ordered_ids": payload.ordered_ids})
    return {"ok": True}


# ── Garmin ─────────────────────────────────────────────────────────────────────

@router.get("/garmin/today")
def garmin_today():
    today = garmin_mod.paris_today().strftime("%Y-%m-%d")
    snap = garmin_mod.get_snapshot(today)
    return {
        "configured": garmin_mod.is_configured(),
        "snapshot": snap,
    }


@router.get("/garmin/week")
def garmin_week():
    return garmin_mod.get_week_snapshots()


@router.get("/garmin/days")
def garmin_days(n: int = QParam(28, ge=1, le=90)):
    """The cached snapshots of the last n calendar days, oldest first. Reads the cache only, never calls Garmin."""
    end = garmin_mod.paris_today()
    start = end - timedelta(days=n - 1)
    return {
        "configured": garmin_mod.is_configured(),
        "snapshots": garmin_mod.get_snapshots_since(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
    }


@router.post("/garmin/refresh")
async def garmin_refresh():
    if not garmin_mod.is_configured():
        raise HTTPException(status_code=503, detail="Garmin credentials not configured")
    try:
        snap = await asyncio.to_thread(garmin_mod.fetch_and_store)
    except garmin_mod.GarminLoginError as exc:
        raise HTTPException(status_code=502, detail=f"Garmin login failed: {exc}")
    await broadcaster.publish("garmin_synced", {"date": snap.get("date")})
    return snap


@router.get("/garmin/activities")
async def garmin_activities(limit: int = 3, refresh: bool = False):
    """List the most-recent activities. Auto-syncs if the cache is empty.

    refresh=true forces a Garmin sync; otherwise we serve cached rows.
    """
    if not garmin_mod.is_configured():
        return {"configured": False, "activities": []}

    rows = garmin_mod.get_recent_activities(limit=max(limit, 10))
    if not rows or refresh:
        await asyncio.to_thread(garmin_mod.fetch_recent_activities, max(limit, 10))
        rows = garmin_mod.get_recent_activities(limit=max(limit, 10))

    return {
        "configured": True,
        "activities": rows[:limit],
    }


@router.post("/garmin/activities/refresh")
async def garmin_activities_refresh():
    if not garmin_mod.is_configured():
        raise HTTPException(status_code=503, detail="Garmin credentials not configured")
    rows = await asyncio.to_thread(garmin_mod.fetch_recent_activities, 10)
    await broadcaster.publish("garmin_activities_synced", {"count": len(rows)})
    return {"count": len(rows)}


@router.get("/garmin/activities/{activity_id}")
async def garmin_activity_detail(activity_id: str, refresh: bool = False):
    if not garmin_mod.is_configured():
        raise HTTPException(status_code=503, detail="Garmin credentials not configured")
    bundle = await asyncio.to_thread(
        garmin_mod.get_activity_full, activity_id, refresh
    )
    if bundle.get("summary") is None and not bundle.get("detail"):
        raise HTTPException(status_code=404, detail="Activity not found")
    return bundle


# ── Weather ────────────────────────────────────────────────────────────────────

@router.get("/weather/forecast")
async def get_weather_forecast():
    return await weather_mod.get_forecast()


@router.get("/weather")
async def get_weather():
    return await weather_mod.get_weather()


# ── News (Al Jazeera RSS, 10-min cache) ────────────────────────────────────────

_news_cache: list[dict] | None = None
_news_cache_ts: float = 0.0
_NEWS_TTL = 600.0
_AJ_RSS = "https://www.aljazeera.com/xml/rss/all.xml"


async def _fetch_news() -> list[dict]:
    global _news_cache, _news_cache_ts
    if _news_cache is not None and time.monotonic() - _news_cache_ts < _NEWS_TTL:
        return _news_cache
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(_AJ_RSS, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            root = ET_XML.fromstring(resp.text)
    except Exception:
        return _news_cache or []

    items = []
    for item in root.findall(".//item")[:10]:
        def _t(tag: str) -> str:
            el = item.find(tag)
            return el.text.strip() if el is not None and el.text else ""
        pub_raw = _t("pubDate")
        try:
            pub_iso = parsedate_to_datetime(pub_raw).isoformat()
        except Exception:
            pub_iso = ""
        items.append({
            "title": _t("title"),
            "url": _t("link"),
            "published": pub_iso,
            "source": "Al Jazeera",
        })

    _news_cache = items
    _news_cache_ts = time.monotonic()
    return items


@router.get("/news")
async def get_news():
    return await _fetch_news()


# ── Event templates ────────────────────────────────────────────────────────────

class EventTemplateCreate(BaseModel):
    title: str
    color: str = "#5AA2FF"


@router.get("/calendar/templates")
def list_templates(conn: sqlite3.Connection = Depends(get_user_conn)):
    rows = conn.execute(
        "SELECT * FROM event_templates ORDER BY created_at"
    ).fetchall()
    return [_row(r) for r in rows]


@router.post("/calendar/templates")
def create_template(
    payload: EventTemplateCreate,
    conn: sqlite3.Connection = Depends(get_user_conn),
):
    cur = conn.execute(
        "INSERT INTO event_templates (title, color) VALUES (?, ?)",
        (payload.title.strip(), payload.color),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM event_templates WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return _row(row)


@router.delete("/calendar/templates/{template_id}")
def delete_template(
    template_id: int,
    conn: sqlite3.Connection = Depends(get_user_conn),
):
    conn.execute("DELETE FROM event_templates WHERE id = ?", (template_id,))
    conn.commit()
    return {"ok": True}


# ── Calendar ───────────────────────────────────────────────────────────────────

class CalendarEventCreate(BaseModel):
    title: str
    start_dt: str
    end_dt: str
    all_day: bool = False
    color_id: Optional[str] = None
    recurrence: Optional[list[str]] = None


_WEEKLY_RULE = re.compile(
    r"^RRULE:FREQ=WEEKLY;BYDAY=(MO|TU|WE|TH|FR|SA|SU)(,(MO|TU|WE|TH|FR|SA|SU))*(;UNTIL=\d{8}T\d{6}Z)?$"
)


class CalendarEventPatch(BaseModel):
    end_dt: Optional[str] = None


class CalendarEventMove(BaseModel):
    start_dt: str
    end_dt: str


@router.get("/calendar/today")
def calendar_today():
    return calendar_sync.get_today_events()


@router.get("/calendar/range")
def calendar_range(
    from_dt: str = QParam(..., alias="from"),
    to_dt: str = QParam(..., alias="to"),
):
    return calendar_sync.get_events(from_dt, to_dt)


@router.get("/calendar/status")
async def calendar_status():
    # Trigger a credential check so a revoked refresh-token gets cleaned up
    # immediately and the UI sees connected=false on the next poll.
    if calendar_sync.is_connected():
        await asyncio.to_thread(calendar_sync._get_credentials)
    return {
        "configured": calendar_sync.is_configured(),
        "connected": calendar_sync.is_connected(),
        "last_synced": calendar_sync.get_last_synced(),
    }


@router.post("/calendar/sync")
async def trigger_calendar_sync(background_tasks: BackgroundTasks):
    creds = await asyncio.to_thread(calendar_sync._get_credentials)
    if creds is None:
        # Token missing / revoked — UI should prompt re-auth
        raise HTTPException(
            status_code=401,
            detail="Google Calendar authentication expired. Reconnect from settings.",
        )
    pushed = await asyncio.to_thread(calendar_sync.push_local_events)
    n = await asyncio.to_thread(calendar_sync.sync_events)
    background_tasks.add_task(_pub, "calendar_synced", {})
    return {"synced": n, "pushed": pushed}


@router.post("/calendar/events")
async def create_calendar_event(
    payload: CalendarEventCreate,
    background_tasks: BackgroundTasks,
):
    if payload.recurrence is not None and (
        len(payload.recurrence) != 1 or not _WEEKLY_RULE.match(payload.recurrence[0])
    ):
        raise HTTPException(422, "Recurrence must be one weekly RRULE line, for example RRULE:FREQ=WEEKLY;BYDAY=MO,TU")
    if not calendar_sync.is_connected():
        raise HTTPException(status_code=503, detail="Google Calendar not connected")
    try:
        ev = await asyncio.to_thread(
            calendar_sync.create_event,
            payload.title,
            payload.start_dt,
            payload.end_dt,
            payload.all_day,
            payload.recurrence,
        )
    except calendar_sync.GoogleNotConnected as exc:
        raise HTTPException(503, str(exc)) from exc
    except calendar_sync.GoogleRefused as exc:
        raise HTTPException(502, str(exc)) from exc
    background_tasks.add_task(_pub, "calendar_synced", {})
    return ev


@router.post("/calendar/events/local")
def create_local_calendar_event(
    payload: CalendarEventCreate,
    conn: sqlite3.Connection = Depends(get_user_conn),
):
    """Create a local-only event (not pushed to Google until sync)."""
    import uuid as _uuid
    local_id = f"local_{_uuid.uuid4().hex[:16]}"
    conn.execute(
        """INSERT INTO calendar_events
           (event_id, calendar_id, title, start_dt, end_dt, all_day, color_id, local_only)
           VALUES (?, 'primary', ?, ?, ?, ?, ?, 1)""",
        (
            local_id,
            payload.title.strip(),
            payload.start_dt,
            payload.end_dt,
            1 if payload.all_day else 0,
            payload.color_id,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM calendar_events WHERE event_id = ?", (local_id,)
    ).fetchone()
    return dict(row)


@router.delete("/calendar/events/{event_id}")
async def delete_calendar_event(event_id: str):
    try:
        await asyncio.to_thread(calendar_sync.delete_event, event_id)
    except calendar_sync.EventNotFound as exc:
        raise HTTPException(404, "Event not found") from exc
    except calendar_sync.GoogleNotConnected as exc:
        raise HTTPException(503, str(exc)) from exc
    except calendar_sync.GoogleRefused as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": True}


@router.patch("/calendar/events/{event_id}")
async def patch_calendar_event(
    event_id: str,
    payload: CalendarEventPatch,
    conn: sqlite3.Connection = Depends(get_user_conn),
):
    if payload.end_dt:
        await asyncio.to_thread(calendar_sync.update_event, event_id, payload.end_dt)
    row = conn.execute(
        "SELECT * FROM calendar_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    return dict(row)


@router.patch("/calendar/events/{event_id}/move")
async def move_calendar_event(
    event_id: str,
    payload: CalendarEventMove,
    conn: sqlite3.Connection = Depends(get_user_conn),
):
    try:
        await asyncio.to_thread(calendar_sync.move_event, event_id, payload.start_dt, payload.end_dt)
    except calendar_sync.EventNotFound as exc:
        raise HTTPException(404, "Event not found") from exc
    except calendar_sync.GoogleNotConnected as exc:
        raise HTTPException(503, str(exc)) from exc
    except calendar_sync.GoogleRefused as exc:
        raise HTTPException(502, str(exc)) from exc
    row = conn.execute(
        "SELECT * FROM calendar_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    return dict(row)


# ── Ask Claude ─────────────────────────────────────────────────────────────────

class Turn(BaseModel):
    role: str
    text: str


class AskBody(BaseModel):
    question: str
    history: list[Turn] = []


MAX_HISTORY_TURNS = 20


def _check_history(history: list[Turn]) -> list[dict]:
    """The earlier turns as messages, or a 422 naming the first bad turn. Must run user, assistant, user, ..."""
    if len(history) > MAX_HISTORY_TURNS:
        raise HTTPException(422, f"History is over {MAX_HISTORY_TURNS} turns")
    out = []
    for n, turn in enumerate(history, start=1):
        if turn.role not in ("user", "assistant"):
            raise HTTPException(422, f"History turn {n} has a role other than user or assistant")
        expected = "user" if n % 2 == 1 else "assistant"
        if turn.role != expected:
            raise HTTPException(422, f"History turn {n} must be from {expected}")
        if not turn.text.strip():
            raise HTTPException(422, f"History turn {n} is empty")
        out.append({"role": turn.role, "text": turn.text})
    if out and out[-1]["role"] != "assistant":
        raise HTTPException(422, f"History turn {len(out)} must be followed by an answer from the assistant")
    return out


@router.post("/ask")
async def ask(body: AskBody):
    """Ask Claude about the next 60 days of the calendar; proposals are only returned, never applied."""
    question = body.question.strip()
    if not question:
        raise HTTPException(422, "Question is empty")
    if len(question) > 2000:
        raise HTTPException(422, "Question is over 2000 characters")
    history = _check_history(body.history)
    try:
        return await asyncio.to_thread(ask_claude.ask, question, ask_claude.paris_today(), history)
    except ask_claude.AskError as exc:
        raise HTTPException(exc.status, exc.message) from exc


# ── Evening Summary (lane ES1a) ────────────────────────────────────────────────

class SummarySettings(BaseModel):
    steps_goal: int


@router.get("/summary")
def get_summary(conn: sqlite3.Connection = Depends(get_user_conn)):
    """The newest stored Evening Summary, or null when none is stored yet."""
    return {"summary": evening_summary.newest(conn)}


@router.get("/summary/settings")
def get_summary_settings(conn: sqlite3.Connection = Depends(get_user_conn)):
    row = conn.execute("SELECT monthly_budget FROM budget_config WHERE id = 1").fetchone()
    return {"steps_goal": evening_summary.steps_goal(conn),
            "monthly_budget": None if row is None else row[0]}


@router.put("/summary/settings")
def put_summary_settings(body: SummarySettings, conn: sqlite3.Connection = Depends(get_user_conn)):
    try:
        evening_summary.set_steps_goal(conn, body.steps_goal)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"steps_goal": evening_summary.steps_goal(conn)}


# ── Evening notification (lane ES1b2) ──────────────────────────────────────────

class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscription(BaseModel):
    endpoint: str
    keys: PushKeys


class PushEndpoint(BaseModel):
    endpoint: str


@router.get("/push/key")
def get_push_key():
    try:
        return {"key": push_mod.public_key()}
    except push_mod.PushError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@router.post("/push/subscribe")
def post_push_subscribe(body: PushSubscription, conn: sqlite3.Connection = Depends(get_user_conn)):
    try:
        push_mod.subscribe(conn, body.endpoint, body.keys.p256dh, body.keys.auth)
    except push_mod.PushError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return {"ok": True}


@router.post("/push/unsubscribe")
def post_push_unsubscribe(body: PushEndpoint, conn: sqlite3.Connection = Depends(get_user_conn)):
    push_mod.unsubscribe(conn, body.endpoint)
    return {"ok": True}


@router.post("/push/state")
def post_push_state(body: PushEndpoint, conn: sqlite3.Connection = Depends(get_user_conn)):
    return {"known": push_mod.known(conn, body.endpoint)}


@router.post("/push/test")
def post_push_test(conn: sqlite3.Connection = Depends(get_user_conn)):
    try:
        return push_mod.send_all(conn, "This is a test. " + push_mod.TAIL)
    except push_mod.PushError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
