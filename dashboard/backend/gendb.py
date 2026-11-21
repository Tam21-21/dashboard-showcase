"""research.db access for the generator store (GV3-W1, DECISIONS #467): template versions, each approved batch (a template run
on a group of stocks) with its kept-back draw and years, its theories, and the run each theory stage made.

Six tables, all append-only, in retestdb.py's shape (own connection, BEGIN IMMEDIATE wrapper, foreign keys and WAL, the
database path explicit on every call so a probe can point it at a scratch file).

DDIA check (docs/books/kleppmann_canon.md, second edition):
  Chapter 3 "Event sourcing and CQRS" (line 801, "Events are immutable"): no row here is ever changed or removed. A template that
  is edited is a new row with its own sha256 (research_theories' precedent); the old row stays, because a batch made from it
  must still show the text it ran on. gendb.py changes and removes no row, and probe v1 greps its source for that.
  Chapter 3, normalization (line 447, IDs over free text): a batch names its template by sha256, a theory its batch by id, a
  run link its theory by id. The text of a template or theory is stored once, where it was made.
  Chapter 8 "Single-object and multi-object operations" (line 1965): a batch, its theories and its kept-back draw are one
  BEGIN IMMEDIATE transaction, so a fault on the third theory leaves no batch and no theory behind.
  Chapter 1 "Systems of record and derived data" (line 100): the rounds, the kept strategies and the final test are NOT copied
  into tables here. The search run's artifacts are the record, and the tree read derives from them (planner ruling 2026-10-22).
  This store keeps only what no artifact holds: which template version, which stocks, which draw, which theories, which runs.

GV3-W3b adds research_gen_saved_strategies and research_gen_stops. DDIA check, same book:
  Chapter 3 "Event sourcing and CQRS" (line 801): a saved strategy and a stop are facts that happened, written once and never changed;
  a theory that is judged again is a new theory in a new batch. UNIQUE (gen_theory_id, rank) and UNIQUE (gen_theory_id) make the
  writer idempotent: a repeated write is an IntegrityError, never a second row.
  Chapter 1 "Systems of record and derived data" (line 100): the verdict stays in research_judge_verdicts, the runs in research_runs;
  a saved row holds their ids and the strategy's picks (the one thing no other table keeps by rank), never a copy of the verdict.
  The CHECK on a saved row (exactly one of verdict_id and refusal) keeps a row from saying nothing or two things.

GV3-W4 adds research_gen_batch_controls and widens the stops CHECK. DDIA check, same book:
  Chapter 3 "Event sourcing and CQRS" (line 801): a pause, a resume and a stop are events written once and never changed; a batch's
  state is DERIVED from its newest control row (none or resume: running, pause: paused, stop: stopped), so the history of who
  paused what and when is kept and nothing is overwritten.
  Chapter 8 "Single-object and multi-object operations" (line 1965): record_batch_control reads the state and writes the row in
  ONE BEGIN IMMEDIATE transaction, so two clicks at once cannot both pass the same check.
  The stops table is rebuilt at boot only when its CHECK lacks 'screen' (SQLite cannot widen a CHECK in place): every row is
  copied, the counts are compared, and a mismatch rolls the rebuild back. No row is ever lost, and a second boot finds the new
  CHECK and does nothing.

LIFECYCLE. Fresh store: init_gen_db makes the four empty tables, list_batches answers an empty list. A restart changes
nothing, since every statement is CREATE IF NOT EXISTS. A batch that does not exist makes read_tree raise BatchNotFound.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Union

from research_lab.theories import expand, form, kept_back

from .config import RESEARCH_DB_PATH

DB_PATH = RESEARCH_DB_PATH  # a caller (a probe) passes its own path to every function

SOURCES = ("file_import", "editor")
STAGES = ("screen", "optimise", "walkforward")
STOP_STEPS = ("screen", "optimise", "walkforward", "judge")
CONTROL_ACTIONS = ("pause", "resume", "stop")
_CONTROL_STATE = {"pause": "paused", "resume": "running", "stop": "stopped"}
# The states each action may start from.
_CONTROL_FROM = {"pause": ("running",), "resume": ("paused",), "stop": ("running", "paused")}


def _stops_ddl(head: str) -> str:
    """The stops table's CREATE statement; `head` is the table name, with IF NOT EXISTS for the boot statement."""
    return f"""
    CREATE TABLE {head} (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        gen_theory_id INTEGER NOT NULL UNIQUE REFERENCES research_gen_theories(id),
        step          TEXT NOT NULL CHECK (step IN ('screen','optimise','walkforward','judge')),
        reason        TEXT NOT NULL,
        stopped_at    INTEGER NOT NULL
    )
    """


class GenDbError(Exception):
    """Base class for every refusal this module raises itself."""


class GenDbParentDirectoryMissing(GenDbError):
    """The database path's parent folder does not exist."""


class TemplateNotFound(GenDbError):
    """No saved template has the given sha256."""


class BatchNotFound(GenDbError):
    """No batch has the given id."""


class BatchControlRefused(GenDbError):
    """A pause, resume or stop the batch's current state does not allow. .state and .since are the current state and its time."""

    def __init__(self, message: str, state: str, since: "int | None"):
        super().__init__(message)
        self.state = state
        self.since = since


class BatchRemoveRefused(GenDbError):
    """A removal the batch's state does not allow. .word is 'not-stopped', 'in-flight' or 'judged'; .names are the run ids (in-flight)
    or the display names (judged), empty for not-stopped."""

    def __init__(self, message: str, word: str, names: list):
        super().__init__(message)
        self.word = word
        self.names = names


_DDL = (
    """
    CREATE TABLE IF NOT EXISTS research_gen_templates (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,
        display_name TEXT NOT NULL,
        sha256       TEXT NOT NULL UNIQUE,
        content      TEXT NOT NULL,
        source       TEXT NOT NULL CHECK (source IN ('file_import','editor')),
        created_at   INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_gen_batches (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        template_sha256       TEXT NOT NULL REFERENCES research_gen_templates(sha256),
        universe_id           INTEGER NOT NULL,
        searched_universe_id  INTEGER NOT NULL,
        group_label           TEXT NOT NULL,
        members_json          TEXT NOT NULL,
        kept_back_json        TEXT NOT NULL,
        seed                  INTEGER NOT NULL,
        data_start            TEXT NOT NULL,
        screen_start          TEXT NOT NULL,
        screen_end            TEXT NOT NULL,
        optimise_start        TEXT NOT NULL,
        optimise_end          TEXT NOT NULL,
        later_start           TEXT NOT NULL,
        later_end             TEXT NOT NULL,
        calendar_id           TEXT NOT NULL UNIQUE,
        approved_at           INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_gen_theories (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id     INTEGER NOT NULL REFERENCES research_gen_batches(id),
        theory_name  TEXT NOT NULL,
        display_name TEXT NOT NULL,
        choices_json TEXT NOT NULL,
        theory_toml  TEXT NOT NULL,
        created_at   INTEGER NOT NULL,
        UNIQUE (batch_id, theory_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_gen_theory_runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        gen_theory_id INTEGER NOT NULL REFERENCES research_gen_theories(id),
        stage         TEXT NOT NULL CHECK (stage IN ('screen','optimise','walkforward')),
        run_id        INTEGER NOT NULL UNIQUE,
        created_at    INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_gen_saved_strategies (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        gen_theory_id      INTEGER NOT NULL REFERENCES research_gen_theories(id),
        rank               INTEGER NOT NULL,
        display_name       TEXT NOT NULL,
        picks_json         TEXT NOT NULL,
        optimise_run_id    INTEGER NOT NULL,
        walkforward_run_id INTEGER NOT NULL,
        variation_id       INTEGER NOT NULL,
        ticker             TEXT NOT NULL,
        verdict_id         INTEGER,
        refusal            TEXT,
        saved_at           INTEGER NOT NULL,
        CHECK ((verdict_id IS NULL) != (refusal IS NULL)),
        UNIQUE (gen_theory_id, rank)
    )
    """,
    _stops_ddl("IF NOT EXISTS research_gen_stops"),
    """
    CREATE TABLE IF NOT EXISTS research_gen_batch_controls (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL REFERENCES research_gen_batches(id),
        action   TEXT NOT NULL CHECK (action IN ('pause','resume','stop')),
        at       INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_gen_batch_controls_batch ON research_gen_batch_controls(batch_id, id)",
    """
    CREATE TABLE IF NOT EXISTS research_gen_batch_removals (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id   INTEGER NOT NULL UNIQUE REFERENCES research_gen_batches(id),
        removed_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_gen_theories_batch ON research_gen_theories(batch_id)",
    "CREATE INDEX IF NOT EXISTS ix_gen_theory_runs_theory ON research_gen_theory_runs(gen_theory_id)",
)


def _connect(research_db_path: Union[str, Path]) -> sqlite3.Connection:
    path = Path(research_db_path)
    if not path.parent.is_dir():
        raise GenDbParentDirectoryMissing(f"cannot open research.db at {path}: its parent directory {path.parent} does not exist")
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _read(research_db_path: Union[str, Path, None] = None):
    conn = _connect(research_db_path if research_db_path is not None else DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _transaction(research_db_path: Union[str, Path, None] = None):
    """BEGIN IMMEDIATE, then COMMIT, or ROLLBACK and re-raise on a SQLite error, so a write that fails part way leaves nothing."""
    conn = _connect(research_db_path if research_db_path is not None else DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.execute("COMMIT;")
        except sqlite3.Error:
            conn.execute("ROLLBACK;")
            raise
    finally:
        conn.close()


def init_gen_db(research_db_path: Union[str, Path, None] = None) -> None:
    """Make the tables if they are missing. Idempotent; called once at boot."""
    with _transaction(research_db_path) as conn:
        for statement in _DDL:
            conn.execute(statement)
    _widen_stops_check(research_db_path if research_db_path is not None else DB_PATH)


def _widen_stops_check(research_db_path: Union[str, Path]) -> None:
    """Rebuild research_gen_stops when its CHECK lacks 'screen' (a store made by GV3-W3b), keeping every row. It follows
    researchdb._ensure_research_shared_runs_polymorphic: foreign keys off (the switch cannot change inside a transaction), one
    BEGIN IMMEDIATE, copy into a new table, compare the counts, drop, rename, check the foreign keys, commit. A fresh store or a store
    already rebuilt holds 'screen' in the table's sql and nothing runs. The table's sql is read as text; SQLite keeps the name
    quoted after a rename, and 'screen' is found either way."""
    conn = sqlite3.connect(str(research_db_path), isolation_level=None)
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'research_gen_stops';").fetchone()
        if row is None or "'screen'" in row[0]:
            return
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute("BEGIN IMMEDIATE;")
        try:
            conn.execute("DROP TABLE IF EXISTS research_gen_stops__new;")
            conn.execute(_stops_ddl("research_gen_stops__new"))
            conn.execute("INSERT INTO research_gen_stops__new (id, gen_theory_id, step, reason, stopped_at) "
                         "SELECT id, gen_theory_id, step, reason, stopped_at FROM research_gen_stops;")
            old_rows = conn.execute("SELECT COUNT(*) FROM research_gen_stops;").fetchone()[0]
            new_rows = conn.execute("SELECT COUNT(*) FROM research_gen_stops__new;").fetchone()[0]
            if old_rows != new_rows:
                raise GenDbError(f"the stops rebuild copied {new_rows} rows from a table of {old_rows}: research_gen_stops is unchanged")
            conn.execute("DROP TABLE research_gen_stops;")
            conn.execute("ALTER TABLE research_gen_stops__new RENAME TO research_gen_stops;")
            broken = conn.execute("PRAGMA foreign_key_check;").fetchall()
            if broken:
                raise GenDbError(f"the stops rebuild left {len(broken)} broken foreign keys: research_gen_stops is unchanged")
            conn.execute("COMMIT;")
        except (sqlite3.Error, GenDbError):
            conn.execute("ROLLBACK;")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.close()


def _read_checked(content: str) -> form.Template:
    """The checked Template of a template text, or form's own TheoryFormError. The text is written as <name>.toml in a folder that
    is removed afterwards, because form.read_template reads a path."""
    data = form._parse_toml(content, "template")
    head = data.get("theory")
    name = head.get("name") if isinstance(head, dict) else None
    if not isinstance(name, str):
        raise form.TheoryFormError("missing_part", "theory.name", "the template needs a [theory] name")
    form._check_name(name, "theory.name")  # the name becomes a file name below, so it is checked first
    with tempfile.TemporaryDirectory(prefix="gen-template-") as folder:
        path = Path(folder) / (name + ".toml")
        path.write_bytes(content.encode("utf-8"))
        return form.read_template(path)


def expand_preview(content: str) -> list:
    """Every theory a template text makes, with no write: [{name, display_name, choices, theory_toml}, ...] in the expand command's order.
    Raises form.TheoryFormError for text the form refuses."""
    return [{"name": e.name, "display_name": e.display_name, "choices": dict(e.choices), "theory_toml": e.text}
            for e in expand.expand_template(_read_checked(content))]


def save_template(research_db_path: Union[str, Path], content: str, source: str, now: int) -> dict:
    """Store a template text as a version, by the sha256 of its exact bytes, after form.read_template accepts it.
    The same text again adds no row. Returns {id, name, display_name, sha256, created}. Raises form.TheoryFormError."""
    if source not in SOURCES:
        raise GenDbError(f"source must be one of {SOURCES}, got {source!r}")
    template = _read_checked(content)
    head = template.data["theory"]
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with _transaction(research_db_path) as conn:
        row = conn.execute("SELECT id FROM research_gen_templates WHERE sha256 = ?;", (sha,)).fetchone()
        if row is not None:
            return {"id": row["id"], "name": head["name"], "display_name": head["display_name"], "sha256": sha, "created": False}
        cur = conn.execute(
            "INSERT INTO research_gen_templates (name, display_name, sha256, content, source, created_at) VALUES (?, ?, ?, ?, ?, ?);",
            (head["name"], head["display_name"], sha, content, source, now))
        return {"id": cur.lastrowid, "name": head["name"], "display_name": head["display_name"], "sha256": sha, "created": True}


def record_batch(research_db_path: Union[str, Path], *, template_sha256: str, universe_id: int, searched_universe_id: int,
                 group_label: str, members: list, seed: int, data_start: str, screen: tuple, optimise: tuple, later: tuple,
                 calendar_id: str, now: int) -> int:
    """Write an approved batch: the batch row with its kept-back draw and every value a run needs, and one row per theory the
    template makes, in ONE transaction. `universe_id` is the universe of the whole group, `searched_universe_id` the one the search
    reads (the group less its kept-back stocks); `screen`, `optimise` and `later` are (start, end) dates; `now` is the approval time.
    Returns the batch id. Raises TemplateNotFound, ValueError (a group under 3 stocks), form.TheoryFormError, and
    sqlite3.IntegrityError (a calendar id already used)."""
    kept = kept_back.draw_kept_back(members, seed)
    for label, window in (("screen", screen), ("optimise", optimise), ("later", later)):
        if not (isinstance(window, (tuple, list)) and len(window) == 2):
            raise GenDbError(f"the {label} window must be (start, end), got {window!r}")
    with _transaction(research_db_path) as conn:
        template = conn.execute("SELECT content FROM research_gen_templates WHERE sha256 = ?;", (template_sha256,)).fetchone()
        if template is None:
            raise TemplateNotFound(f"no saved template has sha256 {template_sha256}")
        theories = expand_preview(template["content"])
        cur = conn.execute(
            "INSERT INTO research_gen_batches (template_sha256, universe_id, searched_universe_id, group_label, members_json, "
            "kept_back_json, seed, data_start, screen_start, screen_end, optimise_start, optimise_end, later_start, later_end, "
            "calendar_id, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (template_sha256, universe_id, searched_universe_id, group_label, json.dumps(list(members)), json.dumps(kept), seed,
             data_start, screen[0], screen[1], optimise[0], optimise[1], later[0], later[1], calendar_id, now))
        batch_id = cur.lastrowid
        for theory in theories:
            conn.execute(
                "INSERT INTO research_gen_theories (batch_id, theory_name, display_name, choices_json, theory_toml, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?);",
                (batch_id, theory["name"], theory["display_name"], json.dumps(theory["choices"]), theory["theory_toml"], now))
        return batch_id


def record_theory_run(research_db_path: Union[str, Path], gen_theory_id: int, stage: str, run_id: int, now: int) -> int:
    """Link a run to the theory it tested. A run id can be linked once (UNIQUE). Returns the link id."""
    with _transaction(research_db_path) as conn:
        cur = conn.execute(
            "INSERT INTO research_gen_theory_runs (gen_theory_id, stage, run_id, created_at) VALUES (?, ?, ?, ?);",
            (gen_theory_id, stage, run_id, now))
        return cur.lastrowid


def record_saved_strategy(research_db_path: Union[str, Path], *, gen_theory_id: int, rank: int, display_name: str, picks: dict,
                          optimise_run_id: int, walkforward_run_id: int, variation_id: int, ticker: str,
                          verdict_id: "int | None" = None, refusal: "str | None" = None, now: int) -> int:
    """Save one held-up strategy of a theory (GV3-W3b): its display name, picks, the two runs it was tested in, the cell the judge
    graded (variation_id, ticker), and EXACTLY ONE of the verdict's id and the judge's refusal sentence. Returns the row id.
    Raises GenDbError (before any write) when both or neither are given, sqlite3.IntegrityError for a second row of the same theory
    and rank or an unknown theory."""
    if (verdict_id is None) == (refusal is None):
        raise GenDbError(f"a saved strategy needs exactly one of verdict_id and refusal, got verdict_id={verdict_id!r}, refusal={refusal!r}")
    with _transaction(research_db_path) as conn:
        cur = conn.execute(
            "INSERT INTO research_gen_saved_strategies (gen_theory_id, rank, display_name, picks_json, optimise_run_id, walkforward_run_id, "
            "variation_id, ticker, verdict_id, refusal, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (gen_theory_id, rank, display_name, json.dumps(picks), optimise_run_id, walkforward_run_id, variation_id, ticker,
             verdict_id, refusal, now))
        return cur.lastrowid


def record_stop(research_db_path: Union[str, Path], gen_theory_id: int, step: str, reason: str, now: int) -> int:
    """Record that a theory's run stopped at `step` (screen, optimise, walkforward or judge) and why, in a plain sentence (GV3-W3b).
    A theory stops once: a second stop is an sqlite3.IntegrityError. Returns the row id."""
    with _transaction(research_db_path) as conn:
        cur = conn.execute(
            "INSERT INTO research_gen_stops (gen_theory_id, step, reason, stopped_at) VALUES (?, ?, ?, ?);",
            (gen_theory_id, step, reason, now))
        return cur.lastrowid


def _control_state(conn: sqlite3.Connection, batch_id: int) -> tuple:
    """(state, since) of a batch from its newest control row: no row or resume is running, pause paused, stop stopped."""
    row = conn.execute("SELECT action, at FROM research_gen_batch_controls WHERE batch_id = ? ORDER BY id DESC LIMIT 1;", (batch_id,)).fetchone()
    if row is None:
        return "running", None
    return _CONTROL_STATE[row["action"]], row["at"]


def record_batch_control(research_db_path: Union[str, Path], batch_id: int, action: str, now: int) -> dict:
    """Pause, resume or stop a batch (GV3-W4): one control row, written when the batch's state allows it. Only pause from running,
    resume from paused, stop from running or paused. Returns {"id", "state", "since"}. Raises BatchNotFound, GenDbError (an action
    other than pause, resume or stop; nothing is written) and BatchControlRefused (carries .state and .since)."""
    if action not in CONTROL_ACTIONS:
        raise GenDbError(f"a batch control must be one of {CONTROL_ACTIONS}, got {action!r}")
    with _transaction(research_db_path) as conn:
        if conn.execute("SELECT 1 FROM research_gen_batches WHERE id = ?;", (batch_id,)).fetchone() is None:
            raise BatchNotFound(f"no batch has id {batch_id}")
        state, since = _control_state(conn, batch_id)
        if state not in _CONTROL_FROM[action]:
            raise BatchControlRefused(f"batch {batch_id} is {state}, so it cannot be told to {action}", state, since)
        cur = conn.execute("INSERT INTO research_gen_batch_controls (batch_id, action, at) VALUES (?, ?, ?);", (batch_id, action, now))
        return {"id": cur.lastrowid, "state": _CONTROL_STATE[action], "since": now}


def remove_batch(research_db_path: Union[str, Path], batch_id: int, now: int) -> dict:
    """Remove a batch from view (GD1a) by appending one removal row; nothing is deleted. Returns {"batch_id", "removed_at", "already"}.
    A removed batch returns its existing row and writes nothing. Raises BatchNotFound and BatchRemoveRefused, checked in this order:
    'not-stopped' (the batch is not stopped), 'in-flight' (a linked run is pending or running; .names are the run ids), 'judged' (a saved
    strategy has a verdict; .names are their display names). The reads and the write share one BEGIN IMMEDIATE."""
    with _transaction(research_db_path) as conn:
        if conn.execute("SELECT 1 FROM research_gen_batches WHERE id = ?;", (batch_id,)).fetchone() is None:
            raise BatchNotFound(f"no batch has id {batch_id}")
        done = conn.execute("SELECT removed_at FROM research_gen_batch_removals WHERE batch_id = ?;", (batch_id,)).fetchone()
        if done is not None:
            return {"batch_id": batch_id, "removed_at": done["removed_at"], "already": True}
        state, _since = _control_state(conn, batch_id)
        if state != "stopped":
            raise BatchRemoveRefused(f"batch {batch_id} is {state}, so it cannot be removed", "not-stopped", [])
        live = [r["id"] for r in conn.execute(
            "SELECT rr.id FROM research_gen_theory_runs l JOIN research_gen_theories t ON t.id = l.gen_theory_id "
            "JOIN research_runs rr ON rr.id = l.run_id WHERE t.batch_id = ? AND rr.status IN ('pending','running') ORDER BY rr.id;",
            (batch_id,)).fetchall()]
        if live:
            raise BatchRemoveRefused(f"batch {batch_id} still has {len(live)} runs going", "in-flight", live)
        judged = [r["display_name"] for r in conn.execute(
            "SELECT s.display_name FROM research_gen_saved_strategies s JOIN research_gen_theories t ON t.id = s.gen_theory_id "
            "WHERE t.batch_id = ? AND s.verdict_id IS NOT NULL ORDER BY s.id;", (batch_id,)).fetchall()]
        if judged:
            raise BatchRemoveRefused(f"batch {batch_id} has strategies the judge graded", "judged", judged)
        conn.execute("INSERT INTO research_gen_batch_removals (batch_id, removed_at) VALUES (?, ?);", (batch_id, now))
        return {"batch_id": batch_id, "removed_at": now, "already": False}


def batch_controls(research_db_path: Union[str, Path]) -> dict:
    """{batch_id: {"state", "since"}} for every batch that has a control row, from its newest row. A batch with none is running."""
    with _read(research_db_path) as conn:
        return {r["batch_id"]: {"state": _CONTROL_STATE[r["action"]], "since": r["at"]} for r in conn.execute(
            "SELECT batch_id, action, at FROM research_gen_batch_controls WHERE id IN "
            "(SELECT MAX(id) FROM research_gen_batch_controls GROUP BY batch_id);").fetchall()}


def _batch_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["members"] = json.loads(out["members_json"])
    out["kept_back"] = json.loads(out["kept_back_json"])
    return out


def list_batches(research_db_path: Union[str, Path]) -> list:
    """Every batch, newest approval first."""
    with _read(research_db_path) as conn:
        return [_batch_dict(r) for r in conn.execute(
            "SELECT b.*, x.removed_at AS removed_at FROM research_gen_batches b LEFT JOIN research_gen_batch_removals x ON x.batch_id = b.id "
            "ORDER BY b.approved_at DESC, b.id DESC;").fetchall()]


def read_tree(research_db_path: Union[str, Path], batch_id: int) -> dict:
    """{batch, template, theories: [{..., choices, runs: [{stage, run_id, ...}], saved: [...by rank], stop: {...} or None}]} for one batch.
    Raises BatchNotFound."""
    with _read(research_db_path) as conn:
        batch = conn.execute(
            "SELECT b.*, x.removed_at AS removed_at FROM research_gen_batches b LEFT JOIN research_gen_batch_removals x ON x.batch_id = b.id "
            "WHERE b.id = ?;", (batch_id,)).fetchone()
        if batch is None:
            raise BatchNotFound(f"no batch has id {batch_id}")
        template = conn.execute("SELECT * FROM research_gen_templates WHERE sha256 = ?;", (batch["template_sha256"],)).fetchone()
        theories = []
        for row in conn.execute("SELECT * FROM research_gen_theories WHERE batch_id = ? ORDER BY id;", (batch_id,)).fetchall():
            theory = dict(row)
            theory["choices"] = json.loads(theory["choices_json"])
            theory["runs"] = [dict(r) for r in conn.execute(
                "SELECT * FROM research_gen_theory_runs WHERE gen_theory_id = ? ORDER BY id;", (row["id"],)).fetchall()]
            theory["saved"] = []
            for saved in conn.execute("SELECT * FROM research_gen_saved_strategies WHERE gen_theory_id = ? ORDER BY rank;", (row["id"],)).fetchall():
                entry = dict(saved)
                entry["picks"] = json.loads(entry["picks_json"])
                theory["saved"].append(entry)
            stop = conn.execute("SELECT * FROM research_gen_stops WHERE gen_theory_id = ?;", (row["id"],)).fetchone()
            theory["stop"] = dict(stop) if stop is not None else None
            theories.append(theory)
        return {"batch": _batch_dict(batch), "template": dict(template), "theories": theories}


def list_templates(research_db_path: Union[str, Path]) -> list:
    """The newest saved version of each template name, newest first: [{id, name, display_name, sha256, created_at}] (GV3-W2). A read only."""
    with _read(research_db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, name, display_name, sha256, created_at FROM research_gen_templates "
            "WHERE id IN (SELECT MAX(id) FROM research_gen_templates GROUP BY name) ORDER BY id DESC;").fetchall()]


def template_names(research_db_path: Union[str, Path]) -> dict:
    """{sha256: display_name} for every saved template version, in one SELECT. A read only (NM1c)."""
    with _read(research_db_path) as conn:
        return {r["sha256"]: r["display_name"] for r in conn.execute("SELECT sha256, display_name FROM research_gen_templates;").fetchall()}


def get_template(research_db_path: Union[str, Path], sha256: str) -> dict:
    """One saved template row ({id, name, display_name, sha256, content, source, created_at}) by its sha256. Raises TemplateNotFound."""
    with _read(research_db_path) as conn:
        row = conn.execute("SELECT * FROM research_gen_templates WHERE sha256 = ?;", (sha256,)).fetchone()
    if row is None:
        raise TemplateNotFound(f"no saved template has sha256 {sha256}")
    return dict(row)
