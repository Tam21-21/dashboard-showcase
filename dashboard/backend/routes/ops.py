"""The counters reader: a read-only route over the backend-process counter dictionaries.

Night four (`#381`), lane R, `docs/plans/night/2026-09-09_set4a_night/PLAN.md` section 11
(job O.6a). Plain-English purpose: three places in this backend already count things that go
wrong (a paper order refused, a judge verdict write refused twice, a sweep dispatch refused by
the allow-list guard). Those counts sit in ordinary Python dictionaries inside this running
process and nothing has ever read them back out. This file adds ONE route that reads them and
returns their current values, and nothing else: it never writes anywhere, so it cannot become a
write path for the operations page, which stays parked until October (`#300`).

The four dictionaries this route reads, named once here so a fifth needs only one new line in
`_COUNTER_SOURCES` below:
  - `paperdb.PAPER_MONITOR_COUNTERS`     (paper trading order and reconciliation refusals)
  - `judgedb.JUDGE_MONITOR_COUNTERS`     (judge verdict-store write refusals)
  - `researchdb.SWEEP_GUARD_REFUSAL_COUNTERS` (sweep allow-list guard refusals, one entry per
    refused group letter plus "UNREADABLE")
  - `chain_runner.CHAIN_RUNNER_CRASH_COUNTERS` (lane P1, DECISIONS #432: a chain that crashed past
    every step handler, and a whole runner pass that crashed)
  - `loop_counters.LOOP_CRASH_COUNTERS` (lane DBG-4, DECISIONS #432: a failed pass of each of the
    four background loops in main.py)

This router imports nothing from `research_lab` itself. `chain_runner` does (the universe rule), as
`stage_runner` already did for every route that dispatches a stage; that is the allowed direction.
The boundary `opsdb.assert_no_research_import()` guards at boot (`dashboard/backend/CLAUDE.md` trap
9) is the other one, research code importing `opsdb`, and this router does not import `opsdb` at
all, because it never touches `ops.db`.

No auth dependency, matching every other browser route in this backend (Caddy basic auth handles
humans at the edge, `dashboard/backend/CLAUDE.md`, "How work flows").
"""
from __future__ import annotations

# re: used to find "DICT_NAME[<something>] += 1" lines in a module's own source text, which is
# how this route works out whether a given counter key has ever actually been incremented
# anywhere (see _declared_only_keys below).
import re
# shutil: AC-6 (docs/units/HT/criteria.md, lane HT, set 14 night) reads the real free/total disk
# space through shutil.disk_usage, so the Home page's own disk figure is MEASURED rather than
# typed. Imported as the module (never "from shutil import disk_usage") so a probe that patches
# shutil.disk_usage at the module level is honoured by the call below, which always looks the
# attribute up on this same shutil module object at call time.
import shutil
# datetime: the response's own read_at timestamp, so a caller can tell how fresh the numbers are.
from datetime import datetime, timezone
# Path: used to read a counter module's own .py source file off disk for the scan above.
from pathlib import Path

from fastapi import APIRouter

from .. import config
# The four modules that own a backend-process counter dictionary today. Imported by name, the
# same way routes/judge.py imports judgedb: dashboard/backend modules only, never research_lab.
# chain_runner joined in lane P1 (DECISIONS #432): its crash counters are how its two catch-all
# sites that end no chain record a failure where a person can see it.
from .. import chain_runner
from .. import judgedb
from .. import loop_counters
from .. import paperdb
from .. import researchdb

router = APIRouter(prefix="/api/ops")

# One row per backend-process counter dictionary this route reads: (the module's short name for
# the response's "module" field, the module object itself, the dictionary's attribute name on
# that module). A future fourth dictionary needs one more tuple here and nothing else in this
# file, because everything below enumerates this list rather than naming any individual key.
_COUNTER_SOURCES = (
    ("paperdb", paperdb, "PAPER_MONITOR_COUNTERS"),
    ("judgedb", judgedb, "JUDGE_MONITOR_COUNTERS"),
    ("researchdb", researchdb, "SWEEP_GUARD_REFUSAL_COUNTERS"),
    ("chain_runner", chain_runner, "CHAIN_RUNNER_CRASH_COUNTERS"),
    ("loop_counters", loop_counters, "LOOP_CRASH_COUNTERS"),
)

# Matches "SOME_DICT_NAME[<expression>] += 1" anywhere in a module's source text. The captured
# group is whatever sits inside the brackets: either a quoted string key (e.g. "foo") or a bare
# expression (e.g. a variable name like frozen_letter). Built fresh per call against the
# attribute name asked for, so it never confuses one dict's increment sites with another's.
def _increment_site_pattern(attribute: str) -> "re.Pattern[str]":
    return re.compile(re.escape(attribute) + r"\[([^\]]+)\]\s*\+=\s*1")


def _declared_only_keys(module_source_path: Path, attribute: str, keys) -> dict:
    """Work out, for each key in `keys`, whether this module's own source ever increments it.

    Plain-English: a counter dictionary is a plain Python dict like {"a": 0, "b": 1}. Some keys
    get "+= 1" somewhere in the code (they are LIVE); others are declared for a failure mode this
    build named but never wired (they are DECLARED_ONLY). This reads the .py file's text and
    looks for the increment pattern the registry's own rows already grep for
    (`grep -n 'JUDGE_MONITOR_COUNTERS\\["duplicate_write_refused_count"\\]' dashboard/backend/judgedb.py`
    is the exact human version of what this function does mechanically), so this route's answer
    and the registry's own commands can never quietly disagree.

    A subtlety this function handles on purpose: an increment site can index the dict with a
    VARIABLE rather than a literal string, for example
    `SWEEP_GUARD_REFUSAL_COUNTERS[frozen_letter] += 1` in researchdb.py. That variable could hold
    ANY of the dict's keys at run time, so once a variable-indexed site is seen for a dictionary,
    every key of that SAME dictionary is treated as live (not declared_only), because no key can
    be ruled out from the source text alone. A literal-string site still marks its own exact key
    as live directly, which matters for a dictionary that mixes both shapes.
    """
    source_text = module_source_path.read_text()
    literal_hit_keys = set()
    has_variable_site = False
    for match in _increment_site_pattern(attribute).finditer(source_text):
        expr = match.group(1).strip()
        is_quoted_literal = len(expr) >= 2 and expr[0] == expr[-1] and expr[0] in ("'", '"')
        if is_quoted_literal:
            literal_hit_keys.add(expr[1:-1])
        else:
            has_variable_site = True
    return {key: not (key in literal_hit_keys or has_variable_site) for key in keys}


@router.get("/counters")
def get_counters() -> dict:
    """Read the backend-process counter dictionaries live, on every call.

    Read-only: nothing here writes to any file or table. Every value is read straight off the
    dictionary OBJECT each request (never a cached copy taken once at import time), which is what
    lets a counter incremented mid-session show the new value on the very next call: this route
    holds no state of its own between requests.
    """
    counters = []
    for module_name, module_obj, attribute in _COUNTER_SOURCES:
        counter_dict = getattr(module_obj, attribute)
        module_source_path = Path(module_obj.__file__)
        declared_only_by_key = _declared_only_keys(module_source_path, attribute, counter_dict.keys())
        for key, value in counter_dict.items():
            counters.append({
                "module": module_name,
                "id": key,
                "value": value,
                "declared_only": declared_only_by_key[key],
            })
    return {
        "read_at": datetime.now(timezone.utc).isoformat(),
        "counters": counters,
    }


# =============================================================================
# GET /api/ops/signals (night four set 4b, lane HM, docs/plans/night/2026-09-09_set4b_day/PLAN.md
# section 11): the Home page's one data source.
#
# Plain-English purpose. Home draws a five-node map (Data library, Strategies, Runs, Forward
# testing, Operations) plus an Operations floor bar. Every node's colour and words must come from
# a real count this backend already keeps, never a literal the page invents -- the page exists to
# show "something is not working" the moment it is true, and a node reading green because nobody
# told it about a failure is the one bug this whole unit is built to rule out. This route is where
# that rule lives: it names, once, which counts feed which node (the "nodes" object below), and it
# computes one honest STATE per count (a "signal"):
#   - "empty"        nothing has happened yet for this signal -- green, and not an error.
#   - "ok"            real activity, and this route found nothing worth a look -- green.
#   - "attention"     real activity, and a named count is worth a look -- amber.
#   - "unavailable"   the table this signal needs could not be found -- red.
# The PAGE turns a node's worst signal state into the actual word a person reads (the state-word
# choice is deliberately a page-side job, matching the UI plan's own "the page renders the worst
# state of a node's set"); this route only groups signals under a node and counts.
#
# Import rule (unchanged from the counters route above): paperdb, judgedb and researchdb only,
# nothing from research_lab, so opsdb.assert_no_research_import() keeps passing at boot. One
# consequence of that rule: dashboard/backend/tracker.py owns the exact "runs without a draw
# link" query this route needs (query_runs_without_draw_link), but tracker is a fourth module
# outside the three named above, so rather than import it, that query's text is reproduced here
# verbatim in _runs_needing_attention_signal below, with a comment pointing back at the function
# it mirrors -- a change to tracker.py's own query must be copied here by hand.
# =============================================================================

# sqlite3 is not imported here on purpose: every "is this table missing" question below is asked
# with a direct read of sqlite_master (routes/judge.py's own _table_present, generalised to take a
# table name), never by catching and pattern-matching a failed SELECT's error text. That is a
# root-cause check, not a guess dressed up as one, and it cannot be fooled by an unrelated
# OperationalError that happens to mention a table's name in a different sentence.

# The four words a signal's own "state" can hold. Named as constants so a typo in one of the four
# strings is a Python NameError the moment this file is imported, never a silent mismatch between
# what this route returns and what a probe or the page expects.
STATE_EMPTY = "empty"              # nothing has happened yet for this signal: green, honestly so
STATE_OK = "ok"                    # real activity, nothing this route can see is wrong: green
STATE_ATTENTION = "attention"      # real activity, and a named count is worth a look: amber
STATE_UNAVAILABLE = "unavailable"  # the table this signal needs is missing: red

# HM-R1-N1 (lead's round-1 read, docs/units/HM/reads/read_1.md): this used to be "No data yet",
# reusing the frontend's own NO_DATA text, but that collided with computeNodeDisplay's own reading
# of this exact condition (guard_counters where no counter dictionary anywhere has a live key) as
# "unavailable" -- the design plan's own three lifecycle sentences keep these genuinely separate
# (docs/design/dashboard_ui_plan_2026-09-07.md section 4 rule 3 and the "Lifecycle states"
# paragraph, docs/design/kit_cheatsheet.md rules 3 and 11): "empty" means a table was successfully
# queried and counted zero rows, which is what NO DATA YET is for; "a signal never yet written"
# names the different case where nothing could even be counted, and the kit's own answer for that
# is "an absent value is a dash, never a fake zero" and "a number the page cannot compute is a
# dash". "No data yet" was quietly claiming a verified zero this branch cannot vouch for; the dash
# makes no such claim.
#
# HM-R2-B1 (lead's round-2 read, docs/units/HM/reads/read_2.md): the FIRST fold of this fix used an
# em dash character (chr(8212), Unicode U+2014) here, following
# dashboard/frontend/src/lib/format.ts's displaySymbol as precedent. That precedent was wrong:
# displaySymbol's em dash predates the kit rules and is itself a docs/design/kit_cheatsheet.md rule
# 12 violation ("an em dash anywhere in UI copy" is one of the hard nos), not an authority to build
# on. The kit's OWN absent-value convention is a plain hyphen, in two real places:
# components/kit/StatusBar.tsx's StatusSegment (`{absent ? "-" : value}`) and
# components/kit/SplitBar.tsx (`"-"` in all three of its zero-total branches); commit 44ffea65
# moved StatusBar off the em dash onto the hyphen for exactly this rule on 2026-09-08. Building the
# em dash from its code point at runtime made the problem WORSE, not better: it let this fact reach
# the page while both the authored-text guard and ui_rules_static.sh's own EM_DASH_COUNT stayed
# blind to it, since neither scans for a character a script assembles only after the file is read.
# So this constant is now the kit's own hyphen, written as an ordinary literal with no construction
# trick to defend: every static check that exists to catch this class of mistake can see it
# plainly, which is the whole point of a check.
_ABSENT_VALUE_DASH = "-"

# runs_missing_engine_provenance (docs/ops/ops_monitor_registry.md, the same row name): a run that
# finished AFTER the engine-and-sizing-provenance unit merged but still carries no sizing_method is
# a run a container built before that unit ran, so its provenance is silently absent rather than
# genuinely NULL-by-choice; a run that finished BEFORE the merge is excluded, because sizing_method
# did not exist yet and a NULL there is simply the old world, not a gap. The registry's own SQL for
# this row leaves the cutoff as a named placeholder ("<this unit's merge time, unix seconds>");
# this is that value, read off the merge commit itself rather than guessed:
#   git show -s --format='%ct %cI' 90582f326c1c3a8e1fad4b617f44c7a3d7047d59
#   -> 1788813866 2026-09-07T22:44:26+02:00
# ("Merge lane E: engine and sizing provenance plus the sizing registry (5.6, #376 ruling 4)").
_ENGINE_PROVENANCE_UNIT_MERGED_AT = 1788813866


def _table_present(conn, table_name: str) -> bool:
    """True when `table_name` exists in the SQLite file this connection has open.

    The same root-cause check routes/judge.py's own _table_present already uses, generalised to
    take the table name as an argument: this one route checks five different tables across two
    different database files, where judge.py's version only ever needed to ask about one.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?;",
        (table_name,),
    ).fetchone()
    return row is not None


def _dataset_status_signal(conn) -> dict:
    """Data library's one signal: research_datasets rows grouped by status. A 'failed' row is the
    one count here worth a look; every other status ('pending', 'fetching', 'ready', 'deleted',
    researchdb.py's own DDL comment) is normal pipeline traffic, not a problem this page flags.
    """
    if not _table_present(conn, "research_datasets"):
        return {"state": STATE_UNAVAILABLE, "fact": "Cannot read the data library store",
                "total": 0, "failed": 0}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM research_datasets GROUP BY status;"
    ).fetchall()
    by_status = {r["status"]: r["n"] for r in rows}
    total = sum(by_status.values())
    failed = by_status.get("failed", 0)
    if total == 0:
        state, fact = STATE_EMPTY, "No datasets yet"
    elif failed > 0:
        state, fact = STATE_ATTENTION, f"{total} datasets on file, {failed} failed"
    else:
        state, fact = STATE_OK, f"{total} datasets on file"
    return {"state": state, "fact": fact, "total": total, "failed": failed}


def _strategy_counts_signal(conn) -> dict:
    """Strategies' first signal: research_strategies rows, and how many failed their last mirror
    write (last_mirror_status = 'failed', one of the four values researchdb.py's own CHECK
    constraint names). A failed mirror means the worker cannot see that strategy's file on disk,
    so it is the one count here worth a look; 'pending' / 'stale' / 'synced' are all normal.
    """
    if not _table_present(conn, "research_strategies"):
        return {"state": STATE_UNAVAILABLE, "fact": "Cannot read the strategy catalogue",
                "total": 0, "failed": 0}
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN last_mirror_status = 'failed' THEN 1 ELSE 0 END) AS failed "
        "  FROM research_strategies;"
    ).fetchone()
    total = row["total"]
    failed = row["failed"] or 0  # SUM() over zero matching rows is NULL, not 0: guarded here
    if total == 0:
        state, fact = STATE_EMPTY, "No strategies registered yet"
    elif failed > 0:
        state, fact = STATE_ATTENTION, f"{total} strategies, {failed} failed to mirror"
    else:
        state, fact = STATE_OK, f"{total} strategies registered"
    return {"state": state, "fact": fact, "total": total, "failed": failed}


def _generated_candidates_signal(conn) -> dict:
    """Strategies' second signal: what the generator has produced so far. A 'refused' candidate is
    the generator's own rule working as designed, not a fault (a theory's filler can legitimately
    fail a skeleton's own check), so this signal only ever distinguishes nothing-yet from
    something-has-happened; it never reaches 'attention'.
    """
    if not _table_present(conn, "research_generated_candidates"):
        return {"state": STATE_UNAVAILABLE, "fact": "Cannot read the generator's own record",
                "total": 0, "accepted": 0}
    rows = conn.execute(
        "SELECT decision, COUNT(*) AS n FROM research_generated_candidates GROUP BY decision;"
    ).fetchall()
    by_decision = {r["decision"]: r["n"] for r in rows}
    total = sum(by_decision.values())
    accepted = by_decision.get("accepted", 0)
    refused = by_decision.get("refused", 0)
    if total == 0:
        state, fact = STATE_EMPTY, "No candidates generated yet"
    else:
        state, fact = STATE_OK, f"{accepted} accepted, {refused} refused"
    return {"state": state, "fact": fact, "total": total, "accepted": accepted}


def _run_status_signal(conn) -> dict:
    """Runs' first signal: whether research_runs is reachable, then how many runs exist and how
    many of them succeeded.

    Deliberately NOT researchdb.runs_summary(): that function ALSO joins research_run_stats for
    its own "best Sharpe" figure (a fact this signal never surfaces), so calling it would make this
    signal depend on a second table it has no other reason to touch -- measured directly, driving
    this route against a fresh research.db that had research_runs but not yet research_run_stats
    raised sqlite3.OperationalError: no such table: research_run_stats from inside runs_summary()
    itself. A plain two-column GROUP BY over research_runs alone is both simpler and a strictly
    smaller dependency than the function it would otherwise reuse.
    """
    if not _table_present(conn, "research_runs"):
        return {"state": STATE_UNAVAILABLE, "fact": "Cannot read the run log",
                "total": 0, "succeeded": 0}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM research_runs GROUP BY status;"
    ).fetchall()
    by_status = {r["status"]: r["n"] for r in rows}
    total = sum(by_status.values())
    succeeded = by_status.get("succeeded", 0)
    if total == 0:
        state, fact = STATE_EMPTY, "No runs yet"
    else:
        state, fact = STATE_OK, f"{total} runs, {succeeded} succeeded"
    return {"state": state, "fact": fact, "total": total, "succeeded": succeeded}


def _runs_needing_attention_signal(conn, total_runs: int) -> dict:
    """Runs' second signal: two ops-monitor-registry counts folded into one number worth a look.

    `total_runs` is passed in from _run_status_signal's own count rather than re-derived, so this
    signal reads STATE_EMPTY on exactly the same "nothing has run yet" condition (and never claims
    research_runs is 'unavailable' a second time under a second name: if that table were missing,
    _run_status_signal already said so, and total_runs is 0 here too, which is why this function
    never has to call _table_present(conn, "research_runs") itself).

    runs_without_draw_link: the exact query dashboard/backend/tracker.py's own
    query_runs_without_draw_link(conn) runs (B17/A9 in that function's own docstring), reproduced
    verbatim here rather than imported -- this route's import list is paperdb/judgedb/researchdb
    only (this unit's own brief); a future change to tracker.py's query must be copied here by
    hand, which is why this comment names the source function and file explicitly.

    runs_missing_engine_provenance: docs/ops/ops_monitor_registry.md's own SQL for this row, with
    its named placeholder cutoff filled in by _ENGINE_PROVENANCE_UNIT_MERGED_AT above.
    """
    if total_runs == 0:
        return {"state": STATE_EMPTY, "fact": "No runs yet", "combined": 0}
    without_link = 0
    if _table_present(conn, "research_data_draws"):
        without_link = len(conn.execute(
            "SELECT DISTINCT r.id FROM research_runs r "
            "JOIN research_data_draws d "
            "  ON d.event_kind = 'draw_requested' AND d.decision = 'granted' "
            "  AND d.strategy_id = r.strategy_id AND d.strategy_version = r.strategy_version "
            "  AND d.created_at <= r.created_at "
            " WHERE NOT EXISTS (SELECT 1 FROM research_data_draws l "
            "                    WHERE l.event_kind = 'run_linked' AND l.run_id = r.id);"
        ).fetchall())
    missing_prov = conn.execute(
        "SELECT COUNT(*) AS n FROM research_runs "
        " WHERE status = 'succeeded' AND finished_at > ? AND sizing_method IS NULL;",
        (_ENGINE_PROVENANCE_UNIT_MERGED_AT,),
    ).fetchone()["n"]
    combined = without_link + missing_prov
    if combined > 0:
        state, fact = STATE_ATTENTION, f"{combined} runs need attention"
    else:
        state, fact = STATE_OK, "Nothing needs attention"
    return {"state": state, "fact": fact, "combined": combined}


def _paper_run_counts_signal(paper_conn) -> dict:
    """Forward testing's one signal: how many paper runs are open right now versus already ended
    (paper_runs.ended_at IS NULL is 'running', paperdb.py's own list_running_runs/list_ended_runs
    idiom, reproduced as one grouped count here rather than two row-materialising calls).
    """
    if not _table_present(paper_conn, "paper_runs"):
        return {"state": STATE_UNAVAILABLE, "fact": "Cannot read the paper trading store",
                "running": 0, "ended": 0}
    row = paper_conn.execute(
        "SELECT SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END) AS running, "
        "       SUM(CASE WHEN ended_at IS NOT NULL THEN 1 ELSE 0 END) AS ended "
        "  FROM paper_runs;"
    ).fetchone()
    running = row["running"] or 0
    ended = row["ended"] or 0
    if running + ended == 0:
        state, fact = STATE_EMPTY, "No paper runs yet"
    else:
        state, fact = STATE_OK, f"{running} running, {ended} ended"
    return {"state": state, "fact": fact, "running": running, "ended": ended}


def _worker_liveness_signal(conn) -> dict:
    """Operations' first signal: research_workers, the same table main.py's own periodic sweep
    already maintains (a worker unseen for 90 seconds is flipped to 'offline' there, so this
    signal trusts the stored status rather than re-deriving staleness from last_seen itself).
    """
    if not _table_present(conn, "research_workers"):
        return {"state": STATE_UNAVAILABLE, "fact": "Cannot read the worker registry",
                "total": 0, "online": 0}
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN status = 'online' THEN 1 ELSE 0 END) AS online "
        "  FROM research_workers;"
    ).fetchone()
    total = row["total"]
    online = row["online"] or 0
    if total == 0:
        state, fact = STATE_EMPTY, "No worker has connected yet"
    elif online == 0:
        state, fact = STATE_ATTENTION, "Every known worker is offline"
    else:
        state, fact = STATE_OK, f"{online} of {total} workers online"
    return {"state": state, "fact": fact, "total": total, "online": online}


def _disk_free_signal() -> dict:
    """AC-6 (docs/units/HT/criteria.md, lane HT): the Home page's Operations floor bar wants a
    disk figure, and no existing signal serves one. This is that ninth signal: a real,
    independent read of the free space on the same filesystem this backend's own stores live on
    (`config.PROJECT_ROOT`, the repo root, matching every *_DB_PATH's own parent directory),
    never a literal typed into this file. The figure is honest-absent on a read failure: the kit's
    own dash, never a fake zero (this route's own STATE_UNAVAILABLE convention, matching every
    other signal above).

    Deliberately not tied to a node: disk space is not one of the five pipeline stages, it belongs
    only to the Operations floor bar (HomePage.tsx reads it directly off "signals", never through
    a "nodes" entry), so the "nodes" dict in get_signals below stays exactly the five it always
    named.

    HT round 2, finding HT-R1-SF1: AC-6's own words are "a dash with its reason on hover, never a
    zero and never a guess", and the read failure's own reason was being discarded on the floor
    (the bare `except OSError:` below swallowed it). The dict below now carries an OPTIONAL
    "reason" key on failure only, a plain-English sentence built from `OSError.strerror` (never
    the raw exception text, which can carry a full path -- matching `DisplayUpstreamError`'s own
    "plain-English reason only" rule in routes/display.py). Every other signal in this file omits
    "reason" entirely, so this is additive: nothing downstream that reads only "state"/"fact" is
    affected.
    """
    try:
        usage = shutil.disk_usage(config.PROJECT_ROOT)
    except OSError as exc:
        # A refused read (the disk unmounted, a permissions error): the honest dash, the same
        # constant every other "cannot be read" case on this page uses, never a zero -- but now
        # WITH a plain-English reason a person can read on hover, rather than a silently dropped
        # exception. `strerror` is the OS's own short phrase ("Permission denied", "No such file
        # or directory"); a bare OSError with no strerror falls back to a generic sentence rather
        # than str(exc), which could include the path.
        reason = (
            f"Free disk space could not be read: {exc.strerror}."
            if exc.strerror
            else "Free disk space could not be read: the operating system refused the read."
        )
        return {"state": STATE_UNAVAILABLE, "fact": _ABSENT_VALUE_DASH, "reason": reason}
    free_gb = usage.free / (1024 ** 3)
    # STATE_OK always: free disk space is informational on this floor bar (the mockup's own
    # DISK ... FREE segment reads in the page's dim secondary tone, never a health colour), so
    # this signal never reaches STATE_ATTENTION on its own. A future low-disk alarm is a fresh,
    # named signal, not an overload of this one's meaning.
    return {"state": STATE_OK, "fact": f"{free_gb:.1f}G FREE"}


def _guard_counters_signal(sources=_COUNTER_SOURCES) -> dict:
    """Operations' second signal: the same three backend-process counter dictionaries
    GET /api/ops/counters already reads (see _COUNTER_SOURCES above), folded into one number.

    `sources` defaults to this file's own _COUNTER_SOURCES but takes an override so a probe can
    drive the "no key anywhere is live" branch below directly: every key of every real dictionary
    in this build has at least one live increment site today (paperdb's two keys both are), so
    that branch cannot be exercised against the real modules without a synthetic source list.
    """
    any_live_key = False
    live_total = 0
    for _module_name, module_obj, attribute in sources:
        counter_dict = getattr(module_obj, attribute)
        module_source_path = Path(module_obj.__file__)
        declared_only_by_key = _declared_only_keys(module_source_path, attribute, counter_dict.keys())
        for key, value in counter_dict.items():
            if not declared_only_by_key[key]:
                any_live_key = True
                live_total += value
    if not any_live_key:
        # No key in any of these dictionaries has ever been incremented anywhere in this backend's
        # own source. Reading "0 refusals" here would claim a verified zero this route cannot
        # actually vouch for; state stays STATE_EMPTY (this is still not an error, the store is
        # fine, nothing here is broken), but the fact reads the kit's own absent-value dash rather
        # than a sentence claiming a count that was never actually taken (HM-R1-N1).
        return {"state": STATE_EMPTY, "fact": _ABSENT_VALUE_DASH, "live_total": 0}
    if live_total == 0:
        return {"state": STATE_EMPTY, "fact": "0 refusals logged", "live_total": 0}
    return {"state": STATE_ATTENTION, "fact": f"{live_total} refusals logged", "live_total": live_total}


@router.get("/signals")
def get_signals() -> dict:
    """GET /api/ops/signals: the Home page's one data source (night four set 4b, lane HM).

    Opens one research.db connection and one paper.db connection, computes the eight signals
    above, and returns them under two keys:
      - "signals": one entry per named signal, a state word plus a plain-words fact -- the wire
        contract the brief asks for, nothing more (the extra numbers each _*_signal helper above
        also returns, like `total` or `failed`, are this function's own working numbers for
        "box_facts" below and are stripped out before the response is built).
      - "nodes": which signals feed which of Home's five pipeline nodes (data_library, strategies,
        runs, forward_testing, operations), plus two ready-to-show fact lines per node. The PAGE
        still computes the worst-of-a-node's-signals colour itself; this route only groups and
        counts, per this unit's own report.
    """
    with researchdb.get_conn() as conn, paperdb.get_paper_conn(paperdb.DB_PATH) as paper_conn:
        dataset_status = _dataset_status_signal(conn)
        strategy_counts = _strategy_counts_signal(conn)
        generated_candidates = _generated_candidates_signal(conn)
        run_status = _run_status_signal(conn)
        runs_needing_attention = _runs_needing_attention_signal(conn, run_status["total"])
        paper_run_counts = _paper_run_counts_signal(paper_conn)
        worker_liveness = _worker_liveness_signal(conn)
        guard_counters = _guard_counters_signal()

    # AC-6's ninth signal: outside the researchdb/paperdb connections above (disk_usage touches
    # the filesystem directly, not either database), computed once here.
    disk_free = _disk_free_signal()

    signals = {
        "dataset_status": dataset_status,
        "strategy_counts": strategy_counts,
        "generated_candidates": generated_candidates,
        "run_status": run_status,
        "runs_needing_attention": runs_needing_attention,
        "paper_run_counts": paper_run_counts,
        "worker_liveness": worker_liveness,
        "guard_counters": guard_counters,
        "disk_free": disk_free,
    }

    # Which signals feed which node, declared here (never in the page), plus the exact two short
    # lines each node's box shows. Every number below is read straight off the dicts above -- an
    # honest count, including a real zero -- never a literal typed into this dict by hand.
    nodes = {
        "data_library": {
            "signal_keys": ["dataset_status"],
            "box_facts": [f"{dataset_status['total']} datasets", f"{dataset_status['failed']} failed"],
        },
        "strategies": {
            "signal_keys": ["strategy_counts", "generated_candidates"],
            "box_facts": [
                f"{strategy_counts['total']} strategies",
                f"{generated_candidates['accepted']} generated",
            ],
        },
        "runs": {
            "signal_keys": ["run_status", "runs_needing_attention"],
            "box_facts": [
                f"{run_status['total']} runs",
                f"{runs_needing_attention['combined']} need attention",
            ],
        },
        "forward_testing": {
            "signal_keys": ["paper_run_counts"],
            "box_facts": [f"{paper_run_counts['running']} running", f"{paper_run_counts['ended']} ended"],
        },
        "operations": {
            "signal_keys": ["worker_liveness", "guard_counters"],
            # guard_counters["fact"] is already a short, box-ready phrase ("No data yet" /
            # "0 refusals logged" / "N refusals logged"), so it is reused as-is rather than
            # recomposed here a second time.
            "box_facts": [f"{worker_liveness['online']} workers online", guard_counters["fact"]],
        },
    }

    # The public shape of "signals": state + fact per signal, PLUS an optional "reason" carried
    # through only when the signal's own dict actually has one (today, only a failed disk read
    # from _disk_free_signal sets it). Stripped down here rather than built this way from the
    # start, so every _*_signal helper above stays free to return whatever extra working numbers
    # box_facts needs (like "total" or "failed") without those leaking into the response contract.
    # HT round 3, AC-6 (docs/units/HT/criteria.md): the old two-key comprehension dropped "reason"
    # unconditionally, so a failed disk read's plain-English explanation never reached the wire and
    # the Home page's hover text had nothing to show. Every other signal never sets "reason", so
    # this stays additive for them: their public shape is exactly the same two keys as before.
    public_signals = {
        key: {"state": value["state"], "fact": value["fact"], **({"reason": value["reason"]} if "reason" in value else {})}
        for key, value in signals.items()
    }

    return {
        "read_at": datetime.now(timezone.utc).isoformat(),
        "signals": public_signals,
        "nodes": nodes,
    }
