"""The Evening Summary, worked out and stored (lane ES1a, DECISIONS #496).

One function builds a day's Summary from the source tables, one stores it once, one reads the newest.
The stored row is derived data: a kept copy of what the sources said at 22:00, never read back as a
source. A part with no data is None, never a made-up number. The 22:00 clock is lane ES1t.
"""
from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import calendar_sync
from . import money
from . import userdb

AT = time(22, 0)
GOAL_DEFAULT = 10000
GOAL_MIN = 1000
GOAL_MAX = 60000
GOAL_WORDS = "The step goal must be between 1,000 and 60,000."
DAYS_BACK = 7
# Tamer 2026-11-19, DECISIONS #497: the round-ups are the change of this round-ups account (institution,
# name) between Finary's reads. Later it is his IBKR account.
ROUND_UPS_ACCOUNT = ("Neobank", "Round-ups")


def steps_goal(conn: sqlite3.Connection) -> int:
    """The stored step goal: a whole number inside the bounds, else the default."""
    row = conn.execute("SELECT value FROM user_prefs WHERE key = 'steps_goal'").fetchone()
    if row is None or row[0] is None:
        return GOAL_DEFAULT
    text = str(row[0]).strip()
    if not text.lstrip("-").isdigit():
        return GOAL_DEFAULT
    n = int(text)
    return n if GOAL_MIN <= n <= GOAL_MAX else GOAL_DEFAULT


def set_steps_goal(conn: sqlite3.Connection, n: int) -> None:
    whole = isinstance(n, int) and not isinstance(n, bool)
    if isinstance(n, float) and n.is_integer():
        n, whole = int(n), True
    if not whole or not GOAL_MIN <= n <= GOAL_MAX:
        raise ValueError(GOAL_WORDS)
    conn.execute(
        """INSERT INTO user_prefs (key, value, updated_at) VALUES ('steps_goal', ?, datetime('now'))
           ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (str(n),),
    )
    conn.commit()


def _mean(values: list) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _r2(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)


def _body(conn: sqlite3.Connection, day: date, goal: int) -> tuple[Optional[dict], Optional[str]]:
    first = (day - timedelta(days=DAYS_BACK)).isoformat()
    rows = conn.execute(
        "SELECT date, steps, active_calories, resting_calories FROM garmin_snapshots"
        " WHERE date >= ? AND date <= ? ORDER BY date",
        (first, day.isoformat()),
    ).fetchall()
    by_date = {r[0]: r for r in rows}
    days = [{"d": r[0], "steps": r[1], "rest": r[3], "move": r[2]}
            for r in rows if r[1] is not None and r[2] is not None and r[3] is not None]
    watch = conn.execute("SELECT MAX(date) FROM garmin_snapshots WHERE steps IS NOT NULL").fetchone()[0]
    if not days or days[-1]["d"] != day.isoformat():
        return None, watch
    t = days[-1]
    prior = days[:-1]
    before = by_date.get((day - timedelta(days=1)).isoformat())
    body = {
        "days": days,
        "steps": t["steps"],
        "goal": goal,
        "reached": t["steps"] >= goal,
        "short": goal - t["steps"],
        "share": t["steps"] / goal,
        "prev": before[1] if before is not None else None,
        "avg_steps": _mean([d["steps"] for d in prior]),
        "kcal": t["rest"] + t["move"],
        "rest": t["rest"],
        "move": t["move"],
        "avg_kcal": _mean([d["rest"] + d["move"] for d in prior]),
    }
    return body, watch


def _money(conn: sqlite3.Connection, day: date) -> Optional[dict]:
    flow = money.out_of_accounts(conn, day)
    today, month = flow["today"], flow["month"]
    if today is None or month is None:
        return None
    row = conn.execute("SELECT monthly_budget FROM budget_config WHERE id = 1").fetchone()
    if row is None:
        return None
    budget = row[0]
    dim = calendar.monthrange(day.year, day.month)[1]
    left = dim - day.day + 1
    share = max(0, budget - month["out_before"]) / left
    pace = budget * day.day / dim
    if month["out"] > budget:
        st = "over"
    elif month["out"] > pace * (1 + money.WARN_AHEAD):
        st = "ahead"
    else:
        st = "ok"
    return {
        "today": _r2(today["out"]), "in": _r2(today["in"]), "budget": _r2(budget),
        "before": _r2(month["out_before"]), "spent": _r2(month["out"]), "left": left,
        "share": _r2(share), "gap": _r2(share - today["out"]), "under": today["out"] <= share,
        "pace": _r2(pace), "st": st, "since": month["since"],
        "read_at": flow["read_at"], "bank_as_of": flow["bank_as_of"],
    }


def _plan(conn: sqlite3.Connection, day: date) -> dict:
    nxt = (day + timedelta(days=1)).isoformat()
    events = [{"title": e["title"], "all_day": bool(e["all_day"]), "start": e["start_dt"], "end": e["end_dt"]}
              for e in calendar_sync.get_events(nxt, nxt)]
    first = next((e["title"] for e in events if not e["all_day"]), None)
    todos = [r[0] for r in conn.execute(
        "SELECT text FROM todos WHERE date = ? AND done = 0 ORDER BY position, id", (nxt,))]
    late = [r[0] for r in conn.execute(
        "SELECT text FROM todos WHERE date < ? AND done = 0 ORDER BY date, position, id", (nxt,))]
    return {"events": events, "first": first, "todos": todos, "late": late}


def _round_values(conn: sqlite3.Connection, day: date) -> dict[date, float]:
    """The account's value on each Paris day up to `day`: the value of the day's newest read that has one."""
    values: dict[date, float] = {}
    syncs = conn.execute("SELECT sync_id, read_at FROM money_syncs ORDER BY read_at, sync_id").fetchall()
    for sync_id, read_at in syncs:
        read_day = money._utc(read_at, "read_at").astimezone(money.PARIS).date()
        if read_day > day:
            continue
        rows = conn.execute(
            "SELECT value_eur FROM money_accounts WHERE sync_id = ? AND institution = ? AND name = ?",
            (sync_id, *ROUND_UPS_ACCOUNT)).fetchall()
        if len(rows) > 1:
            raise ValueError("Two round-ups accounts in one read.")
        if rows:
            values[read_day] = rows[0][0]
    return values


def round_ups(conn: sqlite3.Connection, day: date) -> Optional[dict]:
    """The round-ups of `day`: the change of the ROUND_UPS_ACCOUNT value since the newest valued read
    before the day. None when the day has no change. Writes nothing."""
    values = _round_values(conn, day)
    ordered = sorted(values)
    changes = {ordered[i]: values[ordered[i]] - values[ordered[i - 1]] for i in range(1, len(ordered))}
    if day not in changes:
        return None
    days = [changes[d] for d in ordered if d in changes and (d.year, d.month) == (day.year, day.month)]
    return {"today": _r2(changes[day]), "days": [_r2(c) for c in days],
            "month": _r2(sum(days)), "total": _r2(values[day])}


def build(conn: sqlite3.Connection, day: date) -> dict:
    """The Summary of `day`. Writes nothing. A part with no data is None."""
    goal = steps_goal(conn)
    body, watch_last = _body(conn, day, goal)
    mon = _money(conn, day)
    return {
        "day": day.isoformat(), "goal": goal, "body": body, "watch_last": watch_last,
        "money": mon, "round": round_ups(conn, day), "plan": _plan(conn, day),
        "verdict": {"budget": None if mon is None else mon["under"],
                    "steps": None if body is None else body["reached"]},
    }


def store(conn: sqlite3.Connection, day: date, now: datetime) -> bool:
    """Build and keep the day's row once. True when a row was written, False when one was already there."""
    made_at = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "INSERT OR IGNORE INTO evening_summaries (day, made_at, body) VALUES (?, ?, ?)",
        (day.isoformat(), made_at, json.dumps(build(conn, day))),
    )
    conn.commit()
    return cur.rowcount == 1


def tick(now: Optional[datetime] = None) -> list[str]:
    """One look of the 22:00 clock. Stores today's Summary at or after 22:00 Paris, or the day before's
    when that is missing and it is earlier than 22:00 (a server down at 22:00 catches up). Never rewrites
    a row. Returns the days stored by this call as YYYY-MM-DD."""
    zone = ZoneInfo("Europe/Paris")
    now = datetime.now(timezone.utc) if now is None else now
    local = now.astimezone(zone)
    day = local.date() if local.time() >= AT else local.date() - timedelta(days=1)
    conn = userdb.open_rw()
    try:
        exists = conn.execute("SELECT 1 FROM evening_summaries WHERE day = ?", (day.isoformat(),)).fetchone()
        if exists is not None:
            return []
        return [day.isoformat()] if store(conn, day, now) else []
    finally:
        conn.close()


def newest(conn: sqlite3.Connection) -> Optional[dict]:
    row = conn.execute("SELECT day, made_at, body FROM evening_summaries ORDER BY day DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return {**json.loads(row[2]), "day": row[0], "made_at": row[1]}
