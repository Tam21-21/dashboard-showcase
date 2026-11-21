"""Crash counts for the four background loops in main.py (lane DBG-4, DECISIONS #432).

Plain-English purpose. Each background loop in main.py (calendar sync, the research worker sweeper,
the hourly events sweep, the real-money loss monitor) runs forever, and wraps each pass in a broad
except so that one bad pass never stops the loop. DECISIONS #432 allows that only if the failure is
logged in full AND recorded where a person can see it. The log line is the first half. Each
function below is the second half: it adds one to a count that GET /api/ops/counters
(routes/ops.py) reads live, so the Home page's guard-counter figure includes these failures.

The counts live in this process's memory and reset on restart, the same as CHAIN_RUNNER_CRASH_COUNTERS
in chain_runner.py (K-16). That is accepted: a restart is itself visible on the page.

routes/ops.py `_declared_only_keys` reads this module's own source and looks for the literal
`LOOP_CRASH_COUNTERS["<key>"] += 1` line for each key. That is why every increment below is written
as its own literal line, in this file, and not built from a loop.
"""

# One entry per background loop. Keys are the names the operations route reports.
LOOP_CRASH_COUNTERS: dict = {
    # main.py _calendar_sync_task: one pass of the Google Calendar sync raised.
    "calendar_sync_crashes": 0,
    # main.py _evening_summary_task: one minute's look of the 22:00 Summary clock raised.
    "evening_summary_crashes": 0,
    # main.py _research_sweeper: one pass of the research worker offline sweep raised.
    "research_sweeper_crashes": 0,
    # main.py _events_secondary_sweep_task: one hourly events secondary sweep raised.
    "events_secondary_sweep_crashes": 0,
    # main.py _real_loss_monitor_task: one pass of the real-money loss monitor raised outside any
    # single account's own pass.
    "real_loss_monitor_crashes": 0,
    # main.py _ops_sweeper_task (ops_sweeper.guarded_tick): one five-minute pass of the Operations
    # sweeper raised.
    "ops_sweeper_crashes": 0,
    # main.py _generator_runner_loop (GV3-W3b): one 30-second pass of the generator runner raised past its per-theory guard.
    "generator_runner_crashes": 0,
    # main.py _company_profiles_task (UNI1b): one pass of the SEC company-record reads raised.
    "company_profiles_crashes": 0,
    # generator_runner.run_pass_once (GV3-W3b): one theory's step raised; the pass went on to the next theory.
    "generator_theory_crashes": 0,
}


def count_calendar_sync_crash() -> None:
    """main.py _calendar_sync_task calls this when one pass raised."""
    LOOP_CRASH_COUNTERS["calendar_sync_crashes"] += 1


def count_evening_summary_crash() -> None:
    """main.py _evening_summary_task calls this when one pass raised."""
    LOOP_CRASH_COUNTERS["evening_summary_crashes"] += 1


def count_research_sweeper_crash() -> None:
    """main.py _research_sweeper calls this when one pass raised."""
    LOOP_CRASH_COUNTERS["research_sweeper_crashes"] += 1


def count_events_secondary_sweep_crash() -> None:
    """main.py _events_secondary_sweep_task calls this when one hourly tick raised."""
    LOOP_CRASH_COUNTERS["events_secondary_sweep_crashes"] += 1


def count_real_loss_monitor_crash() -> None:
    """main.py _real_loss_monitor_task calls this when one tick raised outside any account's pass."""
    LOOP_CRASH_COUNTERS["real_loss_monitor_crashes"] += 1


def count_ops_sweeper_crash() -> None:
    """ops_sweeper.guarded_tick (run by main.py _ops_sweeper_task) calls this when one pass raised."""
    LOOP_CRASH_COUNTERS["ops_sweeper_crashes"] += 1


def count_generator_runner_crash() -> None:
    """main.py _generator_runner_loop calls this when one whole pass raised."""
    LOOP_CRASH_COUNTERS["generator_runner_crashes"] += 1


def count_generator_theory_crash() -> None:
    """generator_runner.run_pass_once calls this when one theory's step raised and the pass went on to the next theory."""
    LOOP_CRASH_COUNTERS["generator_theory_crashes"] += 1


def count_company_profiles_crash() -> None:
    """main.py _company_profiles_task calls this when one pass raised."""
    LOOP_CRASH_COUNTERS["company_profiles_crashes"] += 1
