"""Static configuration for the EODHD raw fetcher.

Endpoint shapes, the published span caps, per-request call costs, the era
boundaries, and the operational defaults (concurrency, rate, daily cap).

DECISIONS #204's test decides which constants below owe a parameter_provenance.md
entry: a constant is a backtest parameter iff CHANGING it could change what a
backtest reads, never on how the source describes it. Two constants in this file
fail that test and are provenanced under DECISIONS #251: `TRIM_INTRADAY_TO_TRADING_LIFE`
(decides whether ANY intraday window is trimmed at all) and `DEEP_1M_END` (sets
the deep-era's upper bound, so moving it changes exactly which one-minute bars
exist). A third, `FINALITY_MARGIN_DAYS`, lives in tradinglife.py and is
provenanced the same way. Every OTHER constant here -- the request costs, the
daily cap, DISCOVERY_ANCHORS_YEARS, the retry/concurrency defaults -- changes
only how quickly or cheaply the store is filled and never which bars belong in
it, so it stays an operational knob with no provenance entry. That is a
correction of an earlier version of this comment, which cited "the F12-staleness
precedent" for the whole file: F12 (docs/briefs/term_f12_spec_brief.md) ruled
that DISPLAY thresholds -- how a number LOOKS on a screen -- are operational,
never DATA-FETCH thresholds -- what a request WRITES to the store, which is what
the two constants above actually are. The scope was wrong, not the precedent's
existence. Every value here is either verified against the smoke findings or
marked published-not-measured where the brief says so.
"""

import os  # read the staging root + key path from the environment
from datetime import datetime, timezone  # build the era-boundary epochs in UTC

# --- EODHD REST endpoints (BASE + path), all per the live docs / smoke findings ---
BASE = "https://eodhd.com/api"  # common base for every EODHD REST endpoint
EOD_URL = BASE + "/eod/{symbol}"  # per-ticker end-of-day daily history (smoke-verified)
INTRADAY_URL = BASE + "/intraday/{symbol}"  # per-ticker intraday 1m/5m/1h (smoke-verified)
BULK_URL = BASE + "/eod-bulk-last-day/{exchange}"  # whole-exchange EOD/actions (smoke-verified)
USER_URL = BASE + "/user"  # the live usage counter (apiRequests, dailyRateLimit) (smoke-verified)
SYMBOL_LIST_URL = BASE + "/exchange-symbol-list/{exchange}"  # universe list (UNVERIFIED -> verify-endpoints)
FUNDAMENTALS_URL = BASE + "/fundamentals/{symbol}"  # index constituents via *.INDX (UNVERIFIED -> verify-endpoints)

# --- Published per-request span caps (days). The 1m cap is smoke-verified (HTTP 422); ---
# --- the 5m/1h caps are published-not-measured (smoke findings judged them low-risk). ---
CAP_1M_DAYS = 120  # 1-minute window cap per request (HARD, verified via 422)
CAP_5M_DAYS = 600  # 5-minute window cap per request (HARD, verified via 422 on 2026-08-14)
CAP_1H_DAYS = 7200  # 1-hour window cap per request (published; covers 2020->now in one call)

# --- Per-request call cost charged against the daily quota (smoke-verified) ---
COST_INTRADAY = 5  # one intraday (1m/5m/1h) request costs 5 calls
COST_EOD = 1  # one per-ticker EOD request costs ~1 call
COST_BULK = 100  # one whole-exchange bulk request (EOD/splits/dividends) costs 100 calls
COST_USER = 0  # reading the usage counter is accounted as free (snapshot/diff model)
COST_LIST = 1  # the symbol-list / fundamentals reads cost ~1 call (conservative)

SECONDS_PER_DAY = 86400  # convert a day-span cap into a Unix-timestamp span

# --- Era boundaries (the plan's two-era logic) ---
# Deep era = listing -> 2020: only 1-min exists; fetch 1-min in <=120-day windows.
# Recent era = 2020 -> now: native 1h (one request) + native 5m; no deep 1-min here.
DEEP_1M_END = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())  # backtest parameter (DECISIONS #204/#251): the deep-era 1-min upper bound; data_sources.eodhd_deep_1m_end
RECENT_START = DEEP_1M_END  # recent-era native intraday lower bound (EODHD returns from ~Oct 2020)

# Use the daily history already on disk to drop recent-era intraday sub-requests that fall outside
# a ticker's real trading life (see tradinglife.py). Saves the calls AND keeps a reused symbol's
# intraday response — which EODHD serves from the CURRENT company, ignoring the _old suffix its
# end-of-day endpoint honours — out of a retired ticker's file. Set False to restore the old
# behaviour of always requesting the full window.
TRIM_INTRADAY_TO_TRADING_LIFE = True  # backtest parameter (DECISIONS #204/#251): data_sources.eodhd_intraday_trim_enabled

# Confine the whole fetch universe to the six major listing venues (universe.MAJOR_EXCHANGES).
# Tamer's ruling, 2026-09-11: the queue held 60,033 US symbols, of which 28,976 were OTC / pink /
# grey names the research lab will never trade, and the one-minute backfill was spending its daily
# call budget on them. True keeps every major-venue name, ACTIVE AND DELISTED, so survivorship bias
# is unaffected; only off-venue symbols leave the queue. Set False to restore the full US list.
# This is a backtest parameter under DECISIONS #204's test: changing it changes which tickers exist
# in the store, so it carries a parameter_provenance.md entry (data_sources.eodhd_major_venues_only).
MAJOR_VENUES_ONLY = True  # backtest parameter (DECISIONS #204): data_sources.eodhd_major_venues_only

# --- Operational defaults (overridable via CLI flags; not backtest parameters) ---
DEFAULT_CONCURRENCY = 16  # in-flight requests (the plan's "~16 parallel workers")
DEFAULT_REQ_PER_MIN = 1000  # the published per-minute request cap (the rate-limiter ceiling)
DEFAULT_DAILY_CAP = 100000  # the published daily call cap
DEFAULT_DAILY_MARGIN = 2000  # safety margin below the cap (> one reconcile-interval burst at full concurrency)
DEFAULT_RECONCILE_EVERY = 200  # re-sync the local counter to /user every N requests
DEFAULT_RETRY_MAX = 4  # bounded retries for transient HTTP before marking error + re-queue
DEFAULT_HTTP_TIMEOUT_S = 90.0  # per-request timeout (deep 1m windows take 5-20s; headroom for slow ones)

EXCHANGE = "US"  # the only exchange this unit fetches (US equities + ETFs)

# Manifest interval vocabulary (the §6 enum). Time-series products carry a within-file
# time axis; bulk products are cross-sectional (one day, ~50k tickers, no time axis).
TIMESERIES_INTERVALS = ("daily", "1min", "1h", "5min")  # monotonic/duplicate-timestamp checks apply
BULK_INTERVALS = ("bulk_eod", "bulk_splits", "bulk_dividends")  # duplicate-code check instead
ALL_INTERVALS = TIMESERIES_INTERVALS + BULK_INTERVALS  # the full §6 enum
BULK_TICKER = "__BULK_US__"  # the synthetic ticker key for whole-exchange bulk units (§6 grain)

# Job 13 (docs/handoffs/2026-09-04_fetcher/13_job_run_visibility.md, AC-4): the remaining-days
# summary's per-grain call cost. Built from the constants already named above, never a second set
# of numbers that could quietly drift from them. This is not a new backtest parameter and needs no
# parameter_provenance.md entry (DECISIONS #204's own test): it changes nothing a backtest reads,
# only how the run-visibility summary estimates days of fetching left.
COST_BY_INTERVAL = {
    "daily": COST_EOD,
    "1h": COST_INTRADAY,
    "5min": COST_INTRADAY,
    "1min": COST_INTRADAY,
    "bulk_eod": COST_BULK,
    "bulk_splits": COST_BULK,
    "bulk_dividends": COST_BULK,
}


def staging_root() -> str:
    """Return the locked SSD staging root, overridable via EODHD_STAGING_ROOT for tests.

    Defaults to the path locked this session (/mnt/ssd/eodhd). A test points this at a
    temp dir so no live SSD is touched.
    """
    return os.environ.get("EODHD_STAGING_ROOT", "/mnt/ssd/eodhd")  # env override, else the locked root


def env_file() -> str:
    """Return the path to the dotenv file holding EODHD_API_KEY (the smoke-test pattern)."""
    return os.path.expanduser("~/.research-worker.env")  # the worker's secret file (mode 600)


class StagingNotMountedError(RuntimeError):
    """The staging SSD is not mounted. Raised to refuse any command before it touches the root disk."""


def mountpoint() -> str:
    """Return the path that MUST be a real mountpoint before the fetcher touches the staging root.

    Defaults to /mnt/ssd (the SSD's fstab mount, one level above the staging root). A test points
    this at a real mountpoint (e.g. '/') to exercise the mounted branch, or at a plain directory
    to exercise the refusal — independent of the EODHD_STAGING_ROOT override.
    """
    return os.environ.get("EODHD_MOUNTPOINT", "/mnt/ssd")  # env override (tests), else the locked SSD mount


def require_staging_mounted() -> None:
    """Raise StagingNotMountedError unless the staging mount is actually present (os.path.ismount).

    This is the guard against phantom-writing to the root disk when the SSD is detached: when
    /mnt/ssd is unmounted it is an ordinary directory on the root filesystem (same device as its
    parent /mnt), so os.path.ismount is False; when the SSD is mounted it is a distinct device, so
    ismount is True. Pure filesystem (no key, no network), so it runs before anything is created
    under the staging root.
    """
    mp = mountpoint()  # the path that must be a mountpoint
    if not os.path.ismount(mp):  # nothing mounted there -> the SSD is not attached
        raise StagingNotMountedError(
            f"SSD not mounted at {mp}: refusing to touch {staging_root()} (would write to the root disk). "
            f"Run the elevated remount, then retry."
        )
