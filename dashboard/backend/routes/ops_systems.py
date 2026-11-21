"""The two reads behind the Operations page's systems strip and today's log (lane UI18-0).

GET /api/ops/systems: per system its name, where it runs, one word, a tone, the newest fact, when it was
last proven working, and 48 half-hour marks for the last 24 hours (oldest first). GET /api/ops/log:
today's events and state changes (Europe/Paris day), newest first.

Both only read ops.db (written by ops_sweeper.py). routes/ops.py never touches ops.db by its own
docstring, so these reads live here. No auth dependency, like every browser route in this backend.

What a mark means (planner ruling 2026-10-22): "ok" when any observation in that half hour is ok or
idle, "fail" when the half hour has observations and none is ok, "none" when it has none.
The word comes from the newest observation (ok WORKING, idle or quiet IDLE, fail STALE), DOWN from the
monitor's current state, NOT CONNECTED from a disabled monitor.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from .. import opsdb

router = APIRouter(prefix="/api/ops", tags=["ops"])

PARIS = ZoneInfo("Europe/Paris")
_MARK_COUNT = 48
_HALF_HOUR = timedelta(minutes=30)
_TONES = {"WORKING": "good", "IDLE": "quiet", "STALE": "warn", "DOWN": "bad", "NOT CONNECTED": "off"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _half_hour_start(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    return now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)


def _detail(text) -> dict:
    return json.loads(text) if text else {}


@router.get("/systems")
def get_systems() -> dict:
    now = _now()
    last_start = _half_hour_start(now)
    starts = [last_start - _HALF_HOUR * (_MARK_COUNT - 1 - i) for i in range(_MARK_COUNT)]
    first_iso = _iso(starts[0])
    with opsdb.get_ops_conn() as conn:
        monitors = conn.execute(
            "SELECT m.monitor_id, m.display_name, m.enabled, m.config_json, c.current_state, c.last_proven_at "
            "FROM ops_monitors m LEFT JOIN ops_monitor_current c ON c.monitor_id = m.monitor_id "
            "WHERE m.family = 'systems' ORDER BY m.rowid;").fetchall()
        observations = conn.execute(
            "SELECT monitor_id, received_at, outcome, detail_json FROM ops_observations "
            "WHERE received_at >= ? ORDER BY received_at, observation_id;", (first_iso,)).fetchall()
        newest = {row["monitor_id"]: row for row in conn.execute(
            "SELECT o.monitor_id, o.outcome, o.detail_json FROM ops_observations o WHERE o.observation_id = ("
            "SELECT MAX(observation_id) FROM ops_observations WHERE monitor_id = o.monitor_id);").fetchall()}
    by_monitor: dict[str, list] = {}
    for obs in observations:
        by_monitor.setdefault(obs["monitor_id"], []).append(obs)

    systems = []
    for mon in monitors:
        config = json.loads(mon["config_json"] or "{}")
        latest = newest.get(mon["monitor_id"])
        latest_detail = _detail(latest["detail_json"]) if latest else {}
        if not mon["enabled"]:
            word = "NOT CONNECTED"
        elif mon["current_state"] == "DOWN":
            word = "DOWN"
        elif latest is None:
            word = "IDLE"
        elif latest["outcome"] == "fail":
            word = "STALE"
        elif latest["outcome"] == "idle" or latest_detail.get("quiet"):
            word = "IDLE"
        else:
            word = "WORKING"

        buckets: dict[int, list] = {}
        for obs in by_monitor.get(mon["monitor_id"], []):
            index = int((_parse(obs["received_at"]) - starts[0]) // _HALF_HOUR)
            if 0 <= index < _MARK_COUNT:
                buckets.setdefault(index, []).append(obs)
        marks = []
        for i, start in enumerate(starts):
            in_bucket = buckets.get(i, [])
            if not in_bucket:
                mark, finding = "none", None
            else:
                mark = "ok" if any(o["outcome"] in ("ok", "idle") for o in in_bucket) else "fail"
                finding = _detail(in_bucket[-1]["detail_json"]).get("fact")
            marks.append({"start": _iso(start), "mark": mark, "finding": finding})

        reason = config.get("reason") if not mon["enabled"] else latest_detail.get("fact")
        systems.append({
            "name": mon["display_name"], "where": config.get("where"), "word": word, "tone": _TONES[word],
            "reason": reason, "last_proven_at": mon["last_proven_at"], "marks": marks,
        })
    return {"systems": systems}


@router.get("/log")
def get_log() -> dict:
    now = _now().astimezone(PARIS)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_iso = _iso(day_start)
    with opsdb.get_ops_conn() as conn:
        transitions = conn.execute(
            "SELECT t.changed_at, t.new_state, t.reason, m.display_name FROM ops_transitions t "
            "JOIN ops_monitors m ON m.monitor_id = t.monitor_id WHERE t.changed_at >= ?;", (start_iso,)).fetchall()
        observations = conn.execute(
            "SELECT o.received_at, o.detail_json, m.display_name FROM ops_observations o "
            "JOIN ops_monitors m ON m.monitor_id = o.monitor_id WHERE o.received_at >= ? "
            "AND json_extract(o.detail_json, '$.event') IS NOT NULL;", (start_iso,)).fetchall()
    rows = []
    for t in transitions:
        rows.append((t["changed_at"], 1, {"at": t["changed_at"], "system": t["display_name"],
                                          "sentence": t["reason"], "kind": "transition", "state": t["new_state"]}))
    for o in observations:
        rows.append((o["received_at"], 0, {"at": o["received_at"], "system": o["display_name"],
                                           "sentence": _detail(o["detail_json"])["event"], "kind": "event", "state": None}))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    log = []
    for _, _, row in rows:
        row["time"] = _parse(row["at"]).astimezone(PARIS).strftime("%H:%M")
        log.append(row)
    return {"log": log}
