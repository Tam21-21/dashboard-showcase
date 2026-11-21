"""Newsbot dashboard — FastAPI entry point.

Run locally:
  uvicorn dashboard.backend.main:app --reload --port 8001
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import CORS_ORIGINS, EVENTS_DB_PATH
from .routes import auth, connections, emails, life, stream, research, judge, paper, ops, ops_systems, display, real, generator, generator_v3, retest, reserve, money
from .routes import chain as chain_routes  # lane CN, set 16c (`#427`): the chain runner's own control routes
from .sse import broadcaster
from . import calendar_sync
from . import evening_summary
from . import push
from . import catalog  # M4 R13/R14: the strategy reference catalog (boot-parses the indicator file)
from . import eventsdb
from . import opsdb
from . import ops_sweeper
from . import paperdb
from . import judgedb
from . import userdb
from . import researchdb
from . import chain_runner  # lane CN, set 16c (`#427`): the background pass the loop below calls
from . import generator_runner  # lane GV3-W3b (#467): the pass that takes held-up strategies to the judge
from . import loop_counters  # lane DBG-4 (DECISIONS #432): the crash counts the four loops above increment
from . import retestdb
from . import gendb
from . import reservedb
from . import realdb
from . import real_limits
from . import strategy_mirror
from .events import catalog as event_catalog
from .events import company_profiles  # lane UNI1b: the SEC company-record reads the loop below runs
from .events import sweeps as event_sweeps
from .events.fundamentals import write as fundamentals_write
from .events.fundamentals import catalog as fundamentals_catalog
from .researchdb import transaction

class _StructuredFormatter(logging.Formatter):
    """Render the usual line, then append any fields the call site passed via `extra=`.

    WHY THIS EXISTS, measured rather than assumed. The project rule is that log values ride as
    structured fields and are never interpolated into the message string. Call sites in this
    backend already followed that rule before this formatter existed, and every one of those
    fields was being SILENTLY DISCARDED, because the previous format string
    (`"%(asctime)s %(levelname)-7s %(name)s: %(message)s"`) renders only the message and nothing
    else. `extra=` attaches its keys to the LogRecord; it does not make a formatter print them.

    So the tree had a rule, code that obeyed the rule, and a formatter that threw the result away.
    That is worse than either convention on its own: the compliant sites looked correct and logged
    less than the non-compliant ones. Verified empirically against this exact format string before
    the change -- `log.info("x", extra={"worker_id": "w1"})` emitted the bare word and no worker id.

    THIS DOCSTRING DOES NOT COUNT THE CALL SITES, on purpose. A count is a claim about every OTHER
    file in this backend, not about this one, and it stops being true the instant any of those
    files gains or loses one. That is exactly what broke here twice: one fold got the number
    wrong, and the next fold's re-count was right for a different commit than the one the
    docstring meant (git log this file for both). A count is a fact to derive, not a fact to
    freeze. To find today's set: `grep -rn "extra={" dashboard/backend --include="*.py"`, then
    drop any hit that is not the second argument of an actual `log.<level>(...)` call -- an
    f-string that merely contains the literal text `extra={` inside an assertion message (one
    probe has exactly this) is not a logging call and was never a real site.

    The appended form is `key=value`, space separated, sorted so two records with the same fields
    read the same way. Only keys the caller actually passed are appended: everything the logging
    module puts on a record itself is subtracted using a reference record, so this never has to carry
    a hand-maintained list of standard attribute names that would rot the next time Python adds one.
    """

    # Every attribute the logging module itself sets on a record. Derived once, from a real record,
    # rather than typed out: a hardcoded list is exactly the kind that goes stale silently.
    _BUILTIN_ATTRS = frozenset(
        vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    ) | {"message", "asctime", "taskName"}

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)                       # the ordinary line, unchanged
        extras = {k: v for k, v in vars(record).items() if k not in self._BUILTIN_ATTRS}
        if not extras:
            return base                                     # nothing passed: byte-identical to before
        fields = " ".join(f"{k}={extras[k]}" for k in sorted(extras))
        return f"{base} {fields}"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
# Attach the structured formatter to whatever handler basicConfig just installed. Every logger
# that reaches this handler the ordinary way -- `logging.getLogger(__name__)` or a named child
# logger, neither installing its own handler nor setting `propagate=False` -- gets every `extra=`
# field it passes rendered from here on: existing call sites and new ones alike, with no list of
# files to keep in sync. See the class docstring above for how to find today's existing call
# sites; the count is deliberately not repeated here, for the reason stated there.
class _RedactSecretsFilter(logging.Filter):
    """Rewrite the formatted message so the value after a secret-looking name reads <redacted> (UI17b-0).

    Production's journal held the EODHD token in uvicorn's httpx "HTTP Request" lines. The value runs up
    to the next &, space, quote or the end of the message. A name inside a longer word ("tokens=") is left alone.
    """

    _PATTERN = re.compile(
        r"(?<![A-Za-z0-9_])(api_token|token|apikey|api_key|key|secret|password)=[^&\s'\"]*")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self._PATTERN.sub(lambda m: m.group(1) + "=<redacted>", message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RedactSecretsFilter())
    _handler.setFormatter(_StructuredFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
log = logging.getLogger("dashboard")


async def _calendar_sync_task() -> None:
    """Sync Google Calendar immediately, then every 10 minutes."""
    while True:
        try:
            n = await asyncio.to_thread(calendar_sync.sync_events)
            if n > 0:
                await broadcaster.publish("calendar_synced", {})
        except Exception:  # DECISIONS #432: the loop keeper; logged in full and counted
            loop_counters.count_calendar_sync_crash()
            log.exception("Calendar background sync failed")
        await asyncio.sleep(10 * 60)


def _evening_summary_step() -> None:
    """One look of the 22:00 clock, then the notification for a Summary this look stored. A failed send
    is raised after the stored row is kept, so the loop keeper counts and logs it."""
    push.send_for(evening_summary.tick())


async def _evening_summary_task() -> None:
    """Every minute, look whether today's Evening Summary is due and store it once (DECISIONS #496)."""
    while True:
        try:
            await asyncio.to_thread(_evening_summary_step)
        except Exception:  # DECISIONS #432: the loop keeper; logged in full and counted
            loop_counters.count_evening_summary_crash()
            log.exception("Evening summary loop failed")
        await asyncio.sleep(60)


async def _research_sweeper() -> None:
    """Mark workers offline after 90s without contact. Runs every 30s.

    Mirrors the _calendar_sync_task pattern: try/except wrapper so a
    single failure doesn't kill the background task. Per-transition only
    via UPDATE...RETURNING — already-offline workers are not touched.
    """
    while True:
        await asyncio.sleep(30)
        try:
            now = int(time.time())
            cutoff = now - 90
            with transaction() as conn:
                stale = conn.execute(
                    """
                    UPDATE research_workers
                    SET    status='offline'
                    WHERE  status='online' AND last_seen < ?
                    RETURNING id;
                    """,
                    (cutoff,),
                ).fetchall()
            for row in stale:
                await broadcaster.publish(
                    "research.worker_offline",
                    {"worker_id": row["id"], "reason": "heartbeat_timeout"},
                )
        except Exception:  # DECISIONS #432: the loop keeper; logged in full and counted
            loop_counters.count_research_sweeper_crash()
            log.exception("Research sweeper failed")


async def _ops_sweeper_task() -> None:
    """Lane UI18-0: after a first 60 s, every 5 minutes check the six systems and write what was seen
    into ops.db. Same shape as `_research_sweeper`: sleep first, then one guarded pass. `guarded_tick`
    never raises (it logs, counts the crash and tries a failed dashboard observation), so one bad pass
    never kills the loop (DECISIONS #432)."""
    await asyncio.sleep(60)
    while True:
        await ops_sweeper.guarded_tick(datetime.now(timezone.utc))
        await asyncio.sleep(300)


async def _chain_runner_loop() -> None:
    """Lane CN, set 16c (`#427`), spec section 3: one more asyncio loop of the exact same shape
    as `_research_sweeper` right above it (sleep first, then a try/except around one to_thread
    call, so one bad pass never kills the loop). Every 30 seconds it reads every research_chains
    row that is not yet in a terminal state and advances whichever ones have something to do; the
    logic itself lives in chain_runner.py, never here, so this function is only the wiring."""
    while True:
        await asyncio.sleep(30)
        try:
            pass_started = int(time.time())
            await asyncio.to_thread(chain_runner.run_pass_once)
            # Lane P7-0: the pass runs in a worker thread and cannot publish, so the chains it moved
            # are read back by their updated_at and announced here, once, for the Chains page.
            changed = await asyncio.to_thread(chain_runner.chains_changed_since, pass_started)
            if changed:
                await broadcaster.publish("research.chain_changed", {"chain_ids": changed})
        except Exception:  # DECISIONS #432: the loop keeper; logged in full and counted (K-16)
            chain_runner.count_loop_pass_crash()
            log.exception("Chain runner pass failed")


async def _company_profiles_task() -> None:
    """Lane UNI1b: the only reader of SEC company records in production. One pass at boot, then again when a universe
    is made or members are added (company_profiles.wake) or 3,600 seconds after its last pass. The 3,600 is an
    operational dial (#204); it also picks up the universes the stage runner and the generator runner make. A pass reads
    the companies due (members with no profile and no recent failure) in a worker thread; the logic lives in
    events/company_profiles.py."""
    while True:
        try:
            await asyncio.to_thread(company_profiles.run_due_reads, int(time.time()))
        except Exception:  # DECISIONS #432: the loop keeper; logged in full and counted
            loop_counters.count_company_profiles_crash()
            log.exception("Company profile reads pass failed")
        await company_profiles.wait_woken(3600)


async def _generator_runner_loop() -> None:
    """Lane GV3-W3b (#467): one more loop of _chain_runner_loop's shape. Every 30 seconds one pass takes each approved theory's held-up
    strategies one step toward the judge (optimise run, walk-forward run, judge, save); the logic lives in generator_runner.py."""
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(generator_runner.run_pass_once)
        except Exception:  # DECISIONS #432: the loop keeper; logged in full and counted (a theory's own crash is counted in the pass)
            loop_counters.count_generator_runner_crash()
            log.exception("Generator runner pass failed")


# M2.5 brief I: secondary_sweep background task. Cadence per spec brief §5
# (`secondary_sweep_interval_seconds=3600`). The same secondary_sweep runs at
# startup once below — this loop covers a long-lived dashboard process where
# a cancel-then-immediately-redispatch case would otherwise wait for the next
# deploy restart for stranded events to roll back.
_SECONDARY_SWEEP_INTERVAL_SECONDS = 3600


async def _events_secondary_sweep_task() -> None:
    """Run events_sweeps.secondary_sweep every hour (brief I).

    Idempotent per arch §5.2: a sweep over a clean DB sees no candidate
    batches and writes nothing. Failures are logged and the loop continues —
    the next tick retries. The heartbeat reaper is NOT in this loop because
    the EDGAR fetcher runs it under flock at each invocation; the
    dashboard's once-at-startup call below covers a long-lived dashboard
    that has not had a fetcher firing in a while.
    """
    while True:
        await asyncio.sleep(_SECONDARY_SWEEP_INTERVAL_SECONDS)
        try:
            result = await asyncio.to_thread(event_sweeps.secondary_sweep)
            if result.batches_swept:
                log.info(
                    "events.secondary_sweep (hourly): rolled back %d event(s) "
                    "across %d batch(es); wrote %d cancelled_partial_stub(s)",
                    result.events_rolled_back, len(result.batches_swept),
                    result.cancelled_partial_stubs_written,
                )
        except Exception:  # DECISIONS #432: the loop keeper; logged in full and counted
            loop_counters.count_events_secondary_sweep_crash()
            log.exception("events.secondary_sweep hourly tick failed; continuing loop")


# Lane RL, night two (5.1b): how often PASS-7 (the loss-limit monitor) re-evaluates account
# state. An operational dial (#204: "needs only inline justification"), never a section-16
# parameter -- the two LIMITS themselves (real_trading.loss_limit_window and
# real_trading.loss_limit_since_go) are Tamer's own configured values; this number only decides
# how often the check runs. 60 seconds is a short cadence on purpose: for a money-path safety
# pass, the cost of an extra tick is one cheap read of account state, while the cost of a slow
# tick is a real loss compounding unnoticed for longer than it had to.
_REAL_LOSS_MONITOR_INTERVAL_SECONDS = 60


def _real_loss_monitor_now_iso() -> str:
    """UTC now in the one fixed timestamp shape every store and check in this lane's own build
    uses, matching realdb.py's and real_limits.py's own format string exactly.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_real_loss_monitor_tick(adapter) -> None:
    """The synchronous body of one PASS-7 tick (round 1 fold, finding 3): opens its own
    real-trading connection, reads every account that has ever been given a go, and runs one loss
    monitor pass per account. Called only through asyncio.to_thread below, never awaited directly,
    so its plain sqlite3 calls and the adapter's own synchronous network call never block the one
    shared FastAPI event loop that also serves the kill-switch routes.

    Exception classes caught, and why (round 1 fold, finding 2): each account's own try/except
    here catches sqlite3.Error (a genuine database fault: a lock timeout, a disk error) and
    realdb.AdapterCannotDetermine (the adapter's own declared failure to answer
    report_account_value). run_loss_monitor_pass already catches AdapterCannotDetermine INSIDE
    itself and records its own FAILED run for that one case, so this outer catch mainly exists for
    a fault that escapes it, such as the database itself failing mid-statement. Catching per
    account, inside the loop, is what keeps one account's failure from blocking every other
    account's own evaluation for this tick; catching only these two named classes, rather than
    every exception, is what keeps a genuine coding bug from being silently absorbed here instead
    of surfacing.
    """
    with realdb.get_real_conn() as conn:
        account_ids = [
            row["account_id"] for row in
            conn.execute("SELECT DISTINCT account_id FROM real_enabling_records").fetchall()
        ]
        for account_id in account_ids:
            try:
                real_limits.run_loss_monitor_pass(
                    conn, account_id, adapter, _real_loss_monitor_now_iso()
                )
            except (sqlite3.Error, realdb.AdapterCannotDetermine):
                # Structured fields, never interpolated into the message string, so the account
                # is greppable from the log line rather than buried inside free text.
                log.exception(
                    "Real loss monitor pass failed for one account",
                    extra={"account_id": account_id},
                )
                # Record the FAILED run ourselves (completed=0), the same shape
                # run_loss_monitor_pass's own AdapterCannotDetermine branch writes, so
                # real_loss_monitor_age still sees this account's tick was attempted and failed
                # rather than reading as a tick that silently never ran at all.
                try:
                    conn.execute(
                        "INSERT INTO real_loss_monitor_runs (account_id, ran_at, completed, "
                        "account_value, window_drop, since_go_drop, armed) "
                        "VALUES (?, ?, 0, NULL, NULL, NULL, 0)",
                        (account_id, _real_loss_monitor_now_iso()),
                    )
                except sqlite3.Error:
                    # Recording the failure itself failed too (the connection is genuinely
                    # unusable for this account): log it and move on to the next account rather
                    # than raising out of the loop, which would silently skip every account still
                    # to come.
                    log.exception(
                        "Real loss monitor could not record its own failed run",
                        extra={"account_id": account_id},
                    )


async def _real_loss_monitor_task() -> None:
    """PASS-7 (brief AC-L11): re-evaluate both loss limits, independently of whether any order was
    ever submitted, for every account that has ever been given a go (real_enabling_records holds
    at least one row for it, whether or not that record is still the current one). Arms HALT on a
    breach (never flatten, DECISIONS #392 ruling 5); every call still appends one recorded
    run, breach or not (E10's own empty-set and NULL answers).

    No live execution adapter is wired into routes/real.py yet (real trading stays DISABLED,
    section 19 stop condition 2): real.get_adapter() raises _NoAdapterConfigured on every call
    today, so every tick of this loop currently does nothing after that one check. It is wired now,
    rather than left for a later night to remember, so the safety pass is already running the
    moment an adapter exists.

    The one broad except below is the outermost LOOP-KEEPER (round 1 fold, finding 2), matching
    _research_sweeper's own shape: every per-account failure is already narrowed and handled
    inside _run_real_loss_monitor_tick, so reaching this clause means something broke outside any
    single account's own pass (the connection failed to open at all, the account roster query
    itself raised, or the thread hand-off failed). It still only logs, via log.exception, and never
    swallows silently: the next tick, _REAL_LOSS_MONITOR_INTERVAL_SECONDS later, is unaffected and
    keeps trying regardless of what this one broke on.
    """
    while True:
        await asyncio.sleep(_REAL_LOSS_MONITOR_INTERVAL_SECONDS)
        try:
            adapter = real.get_adapter()
        except real._NoAdapterConfigured:
            continue
        try:
            # Round 1 fold, finding 3: the whole pass runs off the event loop on a worker thread.
            await asyncio.to_thread(_run_real_loss_monitor_tick, adapter)
        except Exception:  # DECISIONS #432: the loop keeper; logged in full and counted
            loop_counters.count_real_loss_monitor_crash()
            log.exception("Real loss monitor tick failed outside any single account's own pass")


def _run_boot_pass(name: str, fn):
    """Run one boot pass. A failure stops the boot: it re-raises with the pass name and the original
    error, chained from it (DECISIONS #437 ruling 2, #432). Nothing is absorbed here."""
    try:
        return fn()
    except Exception as e:
        raise RuntimeError(f"boot pass {name} failed: {e}") from e


@asynccontextmanager
async def lifespan(app: FastAPI):
    userdb.init_db()
    researchdb.init_db()
    # M2.5 brief A: initialise events.db (idempotent; CREATE IF NOT EXISTS).
    # Same pattern as researchdb.init_db() above. Required so a fresh server
    # without the events.db file has the schema ready before the brief I
    # startup sweeps below try to query it.
    eventsdb.init_events_db()
    # OPS-3 (ops_build_brief.md §12.2): initialise ops.db, the operations page's own fourth SQLite
    # file (idempotent; CREATE IF NOT EXISTS). Same pattern as eventsdb.init_events_db() above,
    # placed right beside it because both are "create the file if it is missing" boot steps.
    opsdb.init_ops_db()
    # Lane UI18-0: the six monitored systems are rows of configuration, seeded once (INSERT OR IGNORE).
    ops_sweeper.seed_systems(datetime.now(timezone.utc))
    # Paper trading spec brief §11 E1: initialise paper.db, the paper node's own SQLite file
    # (idempotent; CREATE IF NOT EXISTS). Same pattern as opsdb.init_ops_db() above, placed right
    # beside it because both are "create the file if it is missing" boot steps.
    paperdb.init_paper_db()
    # Night one, lane RT (5.1a): initialise the real-trading store (idempotent; CREATE IF NOT
    # EXISTS). Same pattern as paperdb.init_paper_db() above, placed right beside it because both
    # are "create the file if it is missing" boot steps. This build never sends an order and never
    # writes the enabling record; the store simply exists, disabled, from the moment it boots.
    realdb.init_real_db()
    # Night one (#372): the verdict store lives in research.db and was created by no boot step until
    # now (found 2026-09-06: init_judge_db had no caller outside its probes). Idempotent, CREATE IF NOT EXISTS.
    judgedb.init_judge_db()
    # Lane NR (U.7s, set 8 night): the Nautilus retest store also lives in research.db, in
    # judgedb.py's own shape (Tamer's ruling, plan section 15 item 4). Same idempotent pattern,
    # placed right beside the judge store's own init call because both stores share one file.
    retestdb.init_retest_db()
    # Lane GV3-W1 (#467): the generator store (template versions, batches, theories, runs) also lives in research.db; same idempotent init.
    gendb.init_gen_db()
    # Lane RE (6.4, set 9 night): the reserve exam's own record also lives in research.db, in
    # retestdb.py's own shape (plan section 2's lean). Same idempotent pattern, placed right
    # beside the retest store's own init call since both are append-only logs in the same file.
    reservedb.init_reserve_db()
    # pre-M4 Brief A1 (H4): verify the dataset-type registry's invariants at boot.
    # Called EXPLICITLY here, not as an import side effect — a deliberate improvement
    # over the event-type guard (output_schemas.assert_event_type_registry_consistent,
    # which only runs because some boot import happens to pull its module in). Raising
    # here fails dashboard startup loudly if a writer constant or storage kind ever
    # drifts from researchdb.DATASET_TYPE_REGISTRY. Sync call, no try/except: a drift is
    # a code bug that must stop boot, not a best-effort sweep that may fail benignly.
    researchdb.assert_dataset_type_registry_consistent()
    # OPS-3 (§7.1): fail loud at boot if any research-side module has started importing opsdb,
    # which would let monitor state leak into the research data path. Same "raise here, no
    # try/except" discipline as the registry check above: a drift is a code bug that must stop
    # boot, not a best-effort sweep that may fail benignly.
    opsdb.assert_no_research_import()
    # DBG-4 (paper_trading_spec_brief.md section 10.1, "enforced at boot"): the paper node's own
    # import ban, the same fail-loud-at-boot rule as the opsdb check above. No try/except.
    paperdb.assert_no_research_import()
    # M4 R14/D4: parse + cache the indicator reference at boot, so a MISSING committed reference file
    # fails dashboard startup loudly (a build-dependency error), not silently as an empty catalog on the
    # first /strategies/catalog open. Same fail-loud-at-boot discipline as the registry check above.
    catalog.load_indicators()
    # TERM F3: parse + cache the generated VBT-native indicator reference at boot too, same fail-loud
    # discipline: a missing committed vbt_indicators_reference.md stops startup, not an empty catalog.
    catalog.load_vbt_indicators()
    # AC-3 (docs/briefs/lookahead_guardrail_spec_brief.md): parse + cache the look-ahead authority
    # list at boot too, same fail-loud discipline. A missing, empty or malformed
    # lookahead_indicators.md stops startup, so a future-reading indicator can never reach a strategy
    # unflagged because nobody noticed a broken authority file.
    catalog.load_lookahead_indicators()
    # U.4 criterion 6: parse + cache the generated WQA101 alpha reference at boot too, same
    # fail-loud discipline as the VBT-native reference above: a missing committed
    # wqa101_alphas_reference.md stops startup, not an empty alpha list on the first catalog open.
    catalog.load_alpha_reference()
    # M4.5 3b B4.1: parse + cache the R16 template manifests at boot, same fail-loud discipline: a
    # malformed manifest or a manifest missing its skeleton stops startup, not an empty template gallery.
    catalog.load_strategy_templates()
    # Boot passes (DECISIONS #437 ruling 2, #432). Each runs through _run_boot_pass: a failing pass
    # stops the boot and its error names the pass. The init calls above create every table first, so
    # a fresh server (empty data dir) has what each pass reads.
    # M2.5 brief I: the heartbeat reaper + secondary sweep, once at startup. The hourly
    # _events_secondary_sweep_task below keeps it running.
    def _event_sweeps_boot_pass() -> None:
        reap = event_sweeps.reap_orphaned_running_batches()
        if reap.batches_reaped:
            log.info("events.reap_orphaned_running_batches (startup): flipped %d batch(es); "
                     "rolled back %d event(s)",
                     len(reap.batches_reaped), reap.events_rolled_back)
        sweep = event_sweeps.secondary_sweep()
        if sweep.batches_swept:
            log.info("events.secondary_sweep (startup): rolled back %d event(s) "
                     "across %d batch(es); wrote %d cancelled_partial_stub(s)",
                     sweep.events_rolled_back, len(sweep.batches_swept),
                     sweep.cancelled_partial_stubs_written)

    _run_boot_pass("event_sweeps", _event_sweeps_boot_pass)
    # M2.5 brief H: reconcile_event_catalog backstop, once at startup per spec §6.11 + EL30. It brings
    # research.db.research_datasets back into agreement with the events.db active-row state.
    def _event_catalog_boot_pass() -> None:
        reconcile = event_catalog.reconcile_event_catalog()
        if reconcile.series_walked:
            log.info(
                "events.reconcile_event_catalog (startup): walked %d series, "
                "updated %d, orphan_catalog_rows=%d, orphan_batches=%d",
                reconcile.series_walked, reconcile.series_updated,
                len(reconcile.orphan_catalog_rows), len(reconcile.orphan_batches),
            )

    _run_boot_pass("event_catalog", _event_catalog_boot_pass)
    # M3.75 FUND-1 (§3.3 / §4.3): the fundamentals catalog reconcile, then the orphan-batch reaper.
    # reconcile_fundamentals_catalog re-derives the one fundamentals catalog row from the live facts;
    # _reap_orphaned_fundamental_batches CAS-flips a stale-heartbeat 'running' batch to 'failed'.
    # Two separate passes, so a failure in one does not skip the other.
    _run_boot_pass("fundamentals_catalog", fundamentals_catalog.reconcile_fundamentals_catalog)

    def _fundamentals_reaper_boot_pass() -> None:
        reaped = fundamentals_write._reap_orphaned_fundamental_batches()
        if reaped["batches_reaped"]:
            log.info("fundamentals.reap_orphaned (startup): flipped %d batch(es) to failed",
                     len(reaped["batches_reaped"]))

    _run_boot_pass("fundamentals_reaper", _fundamentals_reaper_boot_pass)

    # Brief H: reconcile the strategy disk mirror from the DB (arch §8). The DB is the source of truth.
    def _strategy_mirror_boot_pass() -> None:
        rewritten = strategy_mirror.reconcile_on_startup()
        if rewritten:
            log.info("strategy mirror rewritten on startup: %s", rewritten)

    _run_boot_pass("strategy_mirror", _strategy_mirror_boot_pass)
    await broadcaster.start()
    asyncio.create_task(_calendar_sync_task())
    asyncio.create_task(_evening_summary_task())
    asyncio.create_task(_research_sweeper())
    asyncio.create_task(_events_secondary_sweep_task())
    # Lane RL, night two (5.1b): PASS-7, the real-trading loss monitor. See the task's own
    # docstring above for why this is safe to start even while real trading stays disabled.
    asyncio.create_task(_real_loss_monitor_task())
    # Lane CN, set 16c (`#427`): the chain runner's own pass, the same "one more loop" shape as
    # every task above it.
    asyncio.create_task(_chain_runner_loop())
    asyncio.create_task(_generator_runner_loop())
    asyncio.create_task(_ops_sweeper_task())
    asyncio.create_task(_company_profiles_task())
    try:
        yield
    finally:
        await broadcaster.stop()


app = FastAPI(title="Newsbot Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(connections.router)
app.include_router(emails.router)
app.include_router(life.router)
app.include_router(stream.router)
app.include_router(research.browser_router)
app.include_router(research.worker_router)
app.include_router(judge.router)
# Lane C5 (set 16c, #427, plan amendment 4): the cscv-split-count worker route, registered right
# after judge.router since both come from the same file (the same "registered right after" idiom
# retest.py's and generator.py's own worker_router lines already use here).
app.include_router(judge.worker_router)
# Lane NR (U.7s, set 8 night): the Nautilus retest routes, registered right after judge.router
# since a retest reads the judge's current verdict but never touches its store.
app.include_router(retest.router)
# Set 16b, lane WD (#423): the retest worker route (POST .../{job_id}/result), registered right
# after retest.router since both come from the same file; nothing else in this build touches
# either router's own registration order.
app.include_router(retest.worker_router)
# Lane RE (6.4, set 9 night): the reserve exam routes, registered right after retest.router since
# both are "read the judge's verdict, dispatch through the real stage runner" siblings that touch
# no store the judge itself owns.
app.include_router(reserve.router)
app.include_router(paper.router)
app.include_router(ops.router)
app.include_router(ops_systems.router)
# lane DL (night plan 2026-09-09 section 10): the Data Library Overview page's display-only Alpaca
# bars route, registered after ops.router as the plan asks. It writes nothing and reads no database.
app.include_router(display.router)
# Night one, lane RT (5.1a): the kill switch's route half (arm, disarm, verify, read state). Never
# an order route; the order gate is exercised by this lane's own probes only, not by any route.
app.include_router(real.router)
# Set 6 day, lane GP (U.4 first half): the Generator page's own router, one new file nobody else's
# area touches this set. Browser routes only, no auth dependency, matching judge.router and
# paper.router immediately above (dashboard/backend/CLAUDE.md, "How work flows").
app.include_router(generator.router)
# Lane GV3-W2 (#467): the Generator v3 routes (templates, preview, approve, batches, tree), right after generator.router.
app.include_router(generator_v3.router)
# Set 16b, lane WD (#423): the generator worker routes (the theory-content read and the facts
# relay), registered right after generator.router since both come from the same file.
app.include_router(generator.worker_router)
# Lane CN, set 16c (`#427`): the chain runner's own control routes (create, pick, stop, retry),
# registered right after the generator router since the CHAINS tab lives on the same Generator
# page as the theories/candidates tabs that router already serves.
app.include_router(chain_routes.router)
# Lane MY1: the money store routes (page reads and targets), and the key-guarded snapshot ingest.
app.include_router(money.router)
app.include_router(money.ingest_router)


@app.get("/api/healthz")
def healthz():
    return {"ok": True}


# Static files — serve the Vite build output if present. In dev the Vite server
# runs separately on :5173 (CORS allows it) and this block is a no-op.
_frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _frontend_dist.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=_frontend_dist / "assets"),
        name="assets",
    )

    _NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):  # pragma: no cover - wiring
        # Set 16b, lane WD (#423) finding 1: an unmatched api/ path used to answer 200 with
        # {"error": "not found"} -- this route is registered LAST and only reached when no real
        # route claimed the path, so a 200 here told every caller's raise_for_status() the request
        # had succeeded when the backend genuinely has no handler for it. That silently turned a
        # worker calling a route this backend lacks into a KeyError deep inside run_generator.py
        # (research_lab/worker/jobs/run_generator.py's own theory_resp.json()["content"], which
        # `except httpx.HTTPError` never caught, because raise_for_status() never raised on a 200).
        # A genuine 404 here is what the worker's own except clause needs to see.
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        candidate = _frontend_dist / full_path
        if full_path and candidate.is_file():
            # Hashed assets (JS/CSS) can be cached; HTML must not be cached so
            # the browser always picks up the latest bundle hashes after a deploy.
            is_html = candidate.suffix == ".html"
            return FileResponse(candidate, headers=_NO_CACHE if is_html else {})
        return FileResponse(_frontend_dist / "index.html", headers=_NO_CACHE)
else:
    log.info("frontend/dist not found — skipping SPA static mount (dev mode OK)")
