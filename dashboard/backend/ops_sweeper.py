"""The Operations sweeper: every five minutes it checks six systems and writes what it saw into ops.db.

Lane UI18-0 (LANES row UI18, DECISIONS #472 item 1; spec ops_build_brief.md sections 7.3, 7.4, 7.6,
7.8). The six systems are Worker, Fetcher, Dashboard, Paper broker, Database backup and Retest
engine. Each is one row in ops_monitors (configuration, not code: the seed below is the only list).
A pass has two steps, in this order:

  1. The decay pass. A system that has not been proven for too long is marked DEGRADED, then DOWN,
     even if no probe ran or the probe itself hung. This is what catches a dead sweeper or a probe
     that never answers.
  2. The probe pass. Each due, enabled monitor is probed once (one query or one file stat, run off
     the event loop with a ten second timeout). One observation is written per probe, a transition
     only when the state really changed, and the current row, all in one ops transaction.

Every time stored here is ISO 8601 UTC to the second with a Z ("2026-10-22T21:00:00Z"), so text
order is time order.

A pass that raises is the caller's job to survive (`guarded_tick`): it logs in full, counts the
failure in loop_counters (DECISIONS #432) and tries to write a failed observation on the dashboard
monitor so the page shows it.

This module never imports researchdb: it opens research.db read-only by path, so the operations
store and the research store stay apart (dashboard-backend skill, "The stores").
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from . import config, loop_counters, opsdb

log = logging.getLogger("dashboard")

# Operational dials, not backtest parameters (dashboard-backend skill: the test is "could changing it
# change a backtest's output", and no).
#
# One probe may take ten seconds at most (ops_build_brief.md section 7.8: cheap probes). Longer, and
# the observation is a failure that says so.
PROBE_TIMEOUT_SECONDS = 10
# Raw observations are kept 30 days (brief 7.5: the current row holds the latest value, so old raw
# rows are superseded). Transitions are never pruned. 1,000 rows per pass keeps one pass short.
OBSERVATION_RETENTION_DAYS = 30
PRUNE_BATCH_ROWS = 1000
# A system with an observed fact "idle" counts as fine; a fetch or retest older than this is "none".
IDLE_AFTER_SECONDS = 7 * 86400
# A fetch or retest that finished ok but more than a day ago leaves the system quiet: the page says IDLE
# for it, not WORKING (the mockup shows the Retest engine idle between retests).
QUIET_AFTER_SECONDS = 86400
# The newest database backup may be 26 hours old: the nightly job plus two hours of slack.
BACKUP_MAX_AGE_SECONDS = 26 * 3600

PARIS = "Europe/Paris"

_STATE_OPERATIONAL = "OPERATIONAL"
_STATE_DEGRADED = "DEGRADED"
_STATE_DOWN = "DOWN"


# =============================================================================
# The seed: six rows of configuration.
# =============================================================================
@dataclass(frozen=True)
class _Seed:
    monitor_id: str
    display_name: str
    where: str
    enabled: int
    degraded_decay_seconds: int
    extra: Optional[dict] = None


# Decay windows, one justification each. They count from when the next check was due (cadence plus the
# value here), so a system that is probed every 300 seconds is never called late between two probes.
#  - Worker 180: main.py's _research_sweeper calls a worker offline after 90 seconds; two of those.
#  - Dashboard 600: it checks itself every 300 seconds; two missed checks.
#  - The others 93,600 (26 hours): a daily job plus two hours, the same bar as BACKUP_MAX_AGE_SECONDS.
# The down window is twice the degraded window.
_SEEDS = (
    _Seed("worker", "Worker", "laptop, Docker", 1, 180),
    _Seed("fetcher", "Fetcher", "laptop", 1, 93600),
    _Seed("dashboard", "Dashboard", "server", 1, 600),
    _Seed("paper_broker", "Paper broker", "Alpaca", 0, 93600,
          {"reason": "The Alpaca paper broker is not connected yet (lane P6)"}),
    _Seed("database_backup", "Database backup", "server", 1, 93600),
    _Seed("retest_engine", "Retest engine", "NautilusTrader", 1, 93600),
)
_CADENCE_SECONDS = 300


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def seed_systems(now: datetime, ops_db_path=None) -> None:
    """Insert the six monitor rows once (INSERT OR IGNORE: a second call changes nothing)."""
    stamp = _iso(now)
    with opsdb.ops_transaction(ops_db_path) as conn:
        for seed in _SEEDS:
            config_json = {"probe": seed.monitor_id, "where": seed.where}
            if seed.extra:
                config_json.update(seed.extra)
            conn.execute(
                "INSERT OR IGNORE INTO ops_monitors (monitor_id, family, display_name, kind, enabled, status, "
                "cadence_seconds, degraded_decay_seconds, down_decay_seconds, config_json, created_at, updated_at) "
                "VALUES (?, 'systems', ?, 'probe', ?, 'active', ?, ?, ?, ?, ?, ?);",
                (seed.monitor_id, seed.display_name, seed.enabled, _CADENCE_SECONDS,
                 seed.degraded_decay_seconds, seed.degraded_decay_seconds * 2,
                 json.dumps(config_json), stamp, stamp),
            )


# =============================================================================
# The probes. Each reads with one query or one file stat and returns
# {"outcome": "ok" | "idle" | "fail", "fact": sentence, "last_id": newest item id or None,
#  "event": sentence for a new item or None, "error_text": optional}.
# =============================================================================
@dataclass(frozen=True)
class ProbeContext:
    now: datetime
    research_db_path: Path
    backup_dir: Path


def _research_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _probe_worker(ctx: ProbeContext) -> dict:
    conn = _research_conn(ctx.research_db_path)
    try:
        row = conn.execute("SELECT id FROM research_workers WHERE status = 'online' LIMIT 1;").fetchone()
    finally:
        conn.close()
    if row is None:
        return {"outcome": "fail", "fact": "No worker is online", "last_id": None, "event": None}
    return {"outcome": "ok", "fact": "A worker is online", "last_id": None, "event": None}


def _first_line(text: Optional[str]) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0] if lines else "no reason was recorded"


def _probe_fetcher(ctx: ProbeContext) -> dict:
    since = int((ctx.now - timedelta(seconds=IDLE_AFTER_SECONDS)).timestamp())
    conn = _research_conn(ctx.research_db_path)
    try:
        row = conn.execute(
            "SELECT id, status, error, finished_at FROM research_jobs WHERE kind = 'fetch_dataset' "
            "AND status IN ('succeeded', 'failed') AND finished_at >= ? "
            "ORDER BY finished_at DESC, id DESC LIMIT 1;", (since,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"outcome": "idle", "fact": "No data fetch has finished in the last 7 days", "last_id": None, "event": None}
    if row["status"] == "succeeded":
        quiet = (ctx.now.timestamp() - row["finished_at"]) > QUIET_AFTER_SECONDS
        return {"outcome": "ok", "fact": "The last data fetch succeeded", "last_id": row["id"],
                "event": "A data fetch finished", "quiet": quiet}
    reason = _first_line(row["error"])
    return {"outcome": "fail", "fact": f"The last data fetch failed: {reason}", "last_id": row["id"],
            "event": f"A data fetch failed: {reason}"}


def _probe_dashboard(ctx: ProbeContext) -> dict:
    return {"outcome": "ok", "fact": "The dashboard ran its own check", "last_id": None, "event": None}


_BACKUP_NAME = re.compile(r"^[a-z]+-(\d{4}-\d{2}-\d{2})\.db$")


def _probe_database_backup(ctx: ProbeContext) -> dict:
    newest = None
    newest_mtime = None
    for entry in Path(ctx.backup_dir).iterdir():
        if _BACKUP_NAME.match(entry.name) and entry.is_file():
            mtime = entry.stat().st_mtime
            if newest_mtime is None or mtime > newest_mtime:
                newest, newest_mtime = entry, mtime
    if newest is None:
        return {"outcome": "fail", "fact": "No database backup file was found", "last_id": None, "event": None}
    age_hours = int((ctx.now.timestamp() - newest_mtime) // 3600)
    day = _BACKUP_NAME.match(newest.name).group(1)
    event = f"A new database backup was written ({day})"
    if (ctx.now.timestamp() - newest_mtime) < BACKUP_MAX_AGE_SECONDS:
        return {"outcome": "ok", "fact": f"The newest database backup is {_plural(age_hours, 'hour')} old",
                "last_id": newest.name, "event": event}
    return {"outcome": "fail", "fact": f"The newest database backup is {_plural(age_hours, 'hour')} old",
            "last_id": newest.name, "event": event}


def _probe_retest_engine(ctx: ProbeContext) -> dict:
    since = int((ctx.now - timedelta(seconds=IDLE_AFTER_SECONDS)).timestamp())
    conn = _research_conn(ctx.research_db_path)
    try:
        row = conn.execute(
            "SELECT id, status, could_not_run_reason FROM research_retests ORDER BY id DESC LIMIT 1;").fetchone()
        created = None
        if row is not None:
            created = conn.execute("SELECT created_at FROM research_retests WHERE id = ?;", (row["id"],)).fetchone()[0]
    finally:
        conn.close()
    if row is None or created < since:
        return {"outcome": "idle", "fact": "No retest has run in the last 7 days", "last_id": None, "event": None}
    if row["status"] == "could-not-run":
        reason = _first_line(row["could_not_run_reason"])
        return {"outcome": "fail", "fact": f"The last retest could not run: {reason}", "last_id": row["id"],
                "event": f"A retest could not run: {reason}"}
    return {"outcome": "ok", "fact": "The last retest finished", "last_id": row["id"],
            "event": f"A retest finished ({row['status']})",
            "quiet": (ctx.now.timestamp() - created) > QUIET_AFTER_SECONDS}


# Keyed by the "probe" value in each monitor's config_json. Looked up at call time, so a test can swap one.
PROBES: dict[str, Callable[[ProbeContext], dict]] = {
    "worker": _probe_worker,
    "fetcher": _probe_fetcher,
    "dashboard": _probe_dashboard,
    "database_backup": _probe_database_backup,
    "retest_engine": _probe_retest_engine,
}


# =============================================================================
# Writing: one observation, a transition only on a real state change, and the current row.
# =============================================================================
def _age_words(seconds: float) -> str:
    if seconds >= 3600:
        return _plural(int(seconds // 3600), "hour")
    return _plural(max(int(seconds // 60), 1), "minute")


def _grade_by_age(monitor: sqlite3.Row, last_proven_or_created: datetime, now: datetime) -> Optional[str]:
    """DEGRADED or DOWN when the last proof is older than the window counted from when the next check was due."""
    age = (now - last_proven_or_created).total_seconds()
    cadence = monitor["cadence_seconds"] or 0
    if monitor["down_decay_seconds"] is not None and age > cadence + monitor["down_decay_seconds"]:
        return _STATE_DOWN
    if monitor["degraded_decay_seconds"] is not None and age > cadence + monitor["degraded_decay_seconds"]:
        return _STATE_DEGRADED
    return None


def _write_state(conn: sqlite3.Connection, monitor_id: str, new_state: str, reason: str, now_iso: str,
                 observation_id: int, last_proven_at: Optional[str], next_due_at: Optional[str]) -> None:
    cur = conn.execute("SELECT current_state, last_proven_at, next_due_at FROM ops_monitor_current WHERE monitor_id = ?;",
                       (monitor_id,)).fetchone()
    previous = cur["current_state"] if cur else None
    # A first observation that is simply OPERATIONAL is not a change from anything, so it writes no
    # transition. A first observation that is not OPERATIONAL does (previous_state is NULL, brief 7.2).
    changed = (previous is not None and previous != new_state) or (previous is None and new_state != _STATE_OPERATIONAL)
    if changed:
        conn.execute(
            "INSERT INTO ops_transitions (monitor_id, previous_state, new_state, changed_at, reason, "
            "caused_by_observation_id) VALUES (?, ?, ?, ?, ?, ?);",
            (monitor_id, previous, new_state, now_iso, reason, observation_id))
    proven = last_proven_at if last_proven_at is not None else (cur["last_proven_at"] if cur else None)
    due = next_due_at if next_due_at is not None else (cur["next_due_at"] if cur else None)
    conn.execute(
        "INSERT INTO ops_monitor_current (monitor_id, current_state, last_proven_at, last_observation_id, "
        "current_reason, next_due_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(monitor_id) DO UPDATE SET current_state = excluded.current_state, "
        "last_proven_at = excluded.last_proven_at, last_observation_id = excluded.last_observation_id, "
        "current_reason = excluded.current_reason, next_due_at = excluded.next_due_at;",
        (monitor_id, new_state, proven, observation_id, reason, due))


def _record(ops_db_path, monitor_id: str, now: datetime, result: dict) -> None:
    """Write one probe result: observation, transition if the state changed, current row, in one transaction."""
    now_iso = _iso(now)
    with opsdb.ops_transaction(ops_db_path) as conn:
        monitor = conn.execute("SELECT * FROM ops_monitors WHERE monitor_id = ?;", (monitor_id,)).fetchone()
        previous_obs = conn.execute(
            "SELECT detail_json FROM ops_observations WHERE monitor_id = ? ORDER BY observation_id DESC LIMIT 1;",
            (monitor_id,)).fetchone()
        event = None
        if previous_obs is not None and result.get("last_id") is not None:
            previous_last = json.loads(previous_obs["detail_json"] or "{}").get("last_id")
            if previous_last != result["last_id"]:
                event = result.get("event")
        detail = {"fact": result["fact"], "last_id": result.get("last_id"), "event": event}
        if result.get("quiet"):
            detail["quiet"] = True
        error_text = result.get("error_text")
        cursor = conn.execute(
            "INSERT INTO ops_observations (monitor_id, received_at, outcome, detail_json, error_text) "
            "VALUES (?, ?, ?, ?, ?);",
            (monitor_id, now_iso, result["outcome"], json.dumps(detail), error_text))
        observation_id = cursor.lastrowid
        cur = conn.execute("SELECT last_proven_at FROM ops_monitor_current WHERE monitor_id = ?;", (monitor_id,)).fetchone()
        if result["outcome"] in ("ok", "idle"):
            new_state, proven = _STATE_OPERATIONAL, now_iso
            reason = result["fact"]
        else:
            # A failing probe is DEGRADED at least; if nothing has been proven for longer than the down
            # window the decay grade wins, so the state does not flip between DOWN and DEGRADED.
            basis = _parse_iso(cur["last_proven_at"]) if cur and cur["last_proven_at"] else _parse_iso(monitor["created_at"])
            new_state = _grade_by_age(monitor, basis, now) or _STATE_DEGRADED
            proven = None
            reason = result["fact"]
        next_due = _iso(now + timedelta(seconds=monitor["cadence_seconds"] or _CADENCE_SECONDS))
        _write_state(conn, monitor_id, new_state, reason, now_iso, observation_id, proven, next_due)


async def _decay_pass(now: datetime, *, ops_db_path=None) -> None:
    """Grade every enabled monitor that has a current row by how long ago it was last proven."""
    now_iso = _iso(now)
    with opsdb.ops_transaction(ops_db_path) as conn:
        monitors = conn.execute(
            "SELECT m.*, c.current_state AS cur_state, c.last_proven_at AS cur_proven, "
            "c.last_observation_id AS cur_obs, c.next_due_at AS cur_due "
            "FROM ops_monitors m JOIN ops_monitor_current c ON c.monitor_id = m.monitor_id "
            "WHERE m.enabled = 1 AND m.status = 'active' AND m.kind = 'probe';").fetchall()
        for monitor in monitors:
            basis = _parse_iso(monitor["cur_proven"] or monitor["created_at"])
            graded = _grade_by_age(monitor, basis, now)
            if graded is None or graded == monitor["cur_state"]:
                continue
            if monitor["cur_state"] == _STATE_DOWN and graded == _STATE_DEGRADED:
                continue
            age = _age_words((now - basis).total_seconds())
            reason = f"Nothing has been proven working for {age}"
            _write_state(conn, monitor["monitor_id"], graded, reason, now_iso, monitor["cur_obs"], None, None)


# =============================================================================
# The pass.
# =============================================================================
def _due_monitors(ops_db_path, now: datetime) -> list[sqlite3.Row]:
    with opsdb.get_ops_conn(ops_db_path) as conn:
        return conn.execute(
            "SELECT m.monitor_id, m.config_json FROM ops_monitors m "
            "LEFT JOIN ops_monitor_current c ON c.monitor_id = m.monitor_id "
            "WHERE m.kind = 'probe' AND m.enabled = 1 AND m.status = 'active' AND m.cadence_seconds IS NOT NULL "
            "AND (c.next_due_at IS NULL OR c.next_due_at <= ?) ORDER BY m.rowid;", (_iso(now),)).fetchall()


async def _run_probe(kind: str, ctx: ProbeContext) -> dict:
    probe = PROBES.get(kind)
    if probe is None:
        return {"outcome": "fail", "fact": "This system has no check yet", "last_id": None, "event": None,
                "error_text": f"no probe is registered for '{kind}'"}
    try:
        return await asyncio.wait_for(asyncio.to_thread(probe, ctx), timeout=PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return {"outcome": "fail", "fact": "The check did not answer in time", "last_id": None, "event": None,
                "error_text": f"the check timed out after {PROBE_TIMEOUT_SECONDS} seconds"}
    except Exception as exc:  # DECISIONS #432: isolates one probe, recorded as its failed observation below
        log.exception("Operations probe %s failed", kind)
        return {"outcome": "fail", "fact": "The check raised an error", "last_id": None, "event": None,
                "error_text": f"{type(exc).__name__}: {exc}"}


def prune(now: datetime, *, ops_db_path=None) -> int:
    """Delete up to 1,000 observations older than 30 days that no transition and no current row points at."""
    cutoff = _iso(now - timedelta(days=OBSERVATION_RETENTION_DAYS))
    with opsdb.ops_transaction(ops_db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM ops_observations WHERE observation_id IN ("
            "SELECT o.observation_id FROM ops_observations o WHERE o.received_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM ops_transitions t WHERE t.caused_by_observation_id = o.observation_id) "
            "AND NOT EXISTS (SELECT 1 FROM ops_monitor_current c WHERE c.last_observation_id = o.observation_id) "
            "ORDER BY o.received_at LIMIT ?);", (cutoff, PRUNE_BATCH_ROWS))
        return cursor.rowcount


async def tick(now: datetime, *, ops_db_path=None, research_db_path=None, backup_dir=None) -> None:
    """One sweeper pass: decay pass, then each due enabled monitor's probe, then the prune."""
    ctx = ProbeContext(
        now=now,
        research_db_path=Path(research_db_path) if research_db_path is not None else config.RESEARCH_DB_PATH,
        backup_dir=Path(backup_dir) if backup_dir is not None
        else Path(os.environ.get("BACKUP_DIR") or (config.PROJECT_ROOT.parent / "backups")),
    )
    await _decay_pass(now, ops_db_path=ops_db_path)
    for monitor in _due_monitors(ops_db_path, now):
        kind = json.loads(monitor["config_json"]).get("probe")
        result = await _run_probe(kind, ctx)
        _record(ops_db_path, monitor["monitor_id"], now, result)
    await asyncio.to_thread(prune, now, ops_db_path=ops_db_path)


async def guarded_tick(now: datetime, *, ops_db_path=None, research_db_path=None, backup_dir=None) -> None:
    """Run one pass and never let it raise (DECISIONS #432: the loop keeper).

    A raising pass is logged in full, counted in loop_counters, and recorded as a failed observation on
    the dashboard monitor. If ops.db itself is broken that last write fails too: that is logged and the
    loop goes on.
    """
    try:
        await tick(now, ops_db_path=ops_db_path, research_db_path=research_db_path, backup_dir=backup_dir)
    except Exception as exc:  # DECISIONS #432: the loop keeper; logged in full and counted
        loop_counters.count_ops_sweeper_crash()
        log.exception("Operations sweeper pass failed")
        failure = {"outcome": "fail", "fact": "The Operations sweeper pass failed", "last_id": None, "event": None,
                   "error_text": f"{type(exc).__name__}: {exc}"}
        try:
            _record(ops_db_path, "dashboard", now, failure)
        except (sqlite3.Error, opsdb.OpsDbParentDirectoryMissing):
            log.exception("Operations sweeper could not record its own failure in ops.db")
