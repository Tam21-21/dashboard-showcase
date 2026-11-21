"""Shared constants and HTTP report helpers for the worker + job handlers.

Lives separately from worker.py to avoid a circular import:
  worker.py     imports jobs.fake / jobs.fetch_dataset (dispatch)
  jobs/*.py     imports helpers (report_log, report_*_completed, ...)
  helpers.py    imports nothing from worker / jobs

So jobs and worker both depend on helpers, never on each other.
"""
import asyncio                    # the shared heartbeat task below sleeps and is cancelled through it
import hashlib
import json
import os
import random
import ssl
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Failure contract (spec brief §3.1). Defined ABOVE the environment reads below
# on purpose: the offline probe suite imports these constants, and the env reads
# raise KeyError on any machine without ~/.research-worker.env (CI included).
# These lists are the SINGLE place any member is enumerated. The probes import
# them rather than restating members, so a spec edit cannot strand a probe.
# ---------------------------------------------------------------------------

# Transient: log, back off, continue. Base classes, matched by issubclass —
# TimeoutException and NetworkError cover their eight leaves and will cover any
# leaf a future httpx adds. Decode faults are here because a truncated body is
# ONE physical event; which of the three fires depends only on where the break
# landed (transfer encoding / multibyte sequence / JSON syntax).
TRANSIENT_EXCEPTIONS = (
    httpx.TimeoutException,     # ConnectTimeout, ReadTimeout, WriteTimeout, PoolTimeout
    httpx.NetworkError,         # ReadError, WriteError, ConnectError, CloseError
    httpx.RemoteProtocolError,  # malformed HTTP from a server mid-restart
    httpx.DecodingError,        # corrupt/truncated body from the reverse proxy
    json.JSONDecodeError,       # body decoded to bytes but is not valid JSON
    UnicodeDecodeError,         # body broke mid-multibyte; json.loads decodes first
)

# Permanent: exit non-zero, loudly. Configuration or code, never weather.
PERMANENT_EXCEPTIONS = (
    httpx.UnsupportedProtocol,  # RESEARCH_SERVER_URL lacks a scheme (hand-edited env)
    httpx.LocalProtocolError,   # client-side code defect
    httpx.ProxyError,           # proxy misconfiguration
    httpx.TooManyRedirects,     # redirect loop
    httpx.InvalidURL,           # malformed URL
)

# Provably pre-request: the route cannot have run, so no claim can exist.
# A WHITELIST, not a partition — anything not in it is may-have-claimed, so the
# safe direction is the default and an unlisted fault cannot land in the unsafe
# bucket. Subset of TRANSIENT_EXCEPTIONS by construction.
CONNECT_PHASE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,          # waiting for a pooled connection; no bytes sent
)

# Statuses that mean "try again": a reverse proxy or a restarting upstream.
# Every other non-2xx exits. 2xx is in no set — raise_for_status never raises on
# it, and 200 (a claimed job) / 204 (idle) are the loop's normal outcomes.
TRANSIENT_STATUSES = frozenset({408, 425, 429}) | frozenset(range(500, 600))

# Concrete leaf classes as ready-made instances, so probes have something
# raisable. json.JSONDecodeError takes (msg, doc, pos) and UnicodeDecodeError
# takes five arguments, which is why this is a mapping and not a bare tuple.
_DUMMY_REQUEST = httpx.Request("GET", "https://example.invalid/poll")
TRANSIENT_LEAVES = {
    httpx.ConnectTimeout:    httpx.ConnectTimeout("connect timed out", request=_DUMMY_REQUEST),
    httpx.ReadTimeout:       httpx.ReadTimeout("read timed out", request=_DUMMY_REQUEST),
    httpx.WriteTimeout:      httpx.WriteTimeout("write timed out", request=_DUMMY_REQUEST),
    httpx.PoolTimeout:       httpx.PoolTimeout("pool timed out", request=_DUMMY_REQUEST),
    httpx.ReadError:         httpx.ReadError("read error", request=_DUMMY_REQUEST),
    httpx.WriteError:        httpx.WriteError("write error", request=_DUMMY_REQUEST),
    httpx.ConnectError:      httpx.ConnectError("connect error", request=_DUMMY_REQUEST),
    httpx.CloseError:        httpx.CloseError("close error", request=_DUMMY_REQUEST),
    httpx.RemoteProtocolError: httpx.RemoteProtocolError("server disconnected", request=_DUMMY_REQUEST),
    httpx.DecodingError:     httpx.DecodingError("cannot decode body", request=_DUMMY_REQUEST),
    json.JSONDecodeError:    json.JSONDecodeError("Expecting value", "", 0),
    UnicodeDecodeError:      UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
}

# Verdicts returned by classify_fault.
TRANSIENT    = "transient"
PERMANENT    = "permanent"
NOT_AN_ERROR = "not-an-error"

# Phases. Assigned by the CALL SITE, never derived from type membership — see
# classify_fault's docstring for why that distinction is load-bearing.
PHASE_CONNECT          = "connect"
PHASE_MAY_HAVE_CLAIMED = "may-have-claimed"

# Backoff envelope (§3.2 rule 4). base = POLL_INTERVAL_S, set below.
BACKOFF_CAP_S = 60.0
# Clamped before the shift so 2.0 * 2**n cannot raise OverflowError at n >= 1024;
# the function stays total independent of its caller.
_BACKOFF_MAX_N = 64

def _effective_cause(exc: BaseException) -> BaseException | None:
    """The next link in Python's EFFECTIVE-cause chain (Cowork R-CERT).

    Per node: an explicit `__cause__` wins when set; otherwise `__context__`.

    ONE DOCUMENTED DEVIATION from the R-CERT text, forced by measurement and
    flagged to Cowork rather than taken silently. R-CERT says to stop at a node
    whose `__suppress_context__` is true. The live httpcore node sets
    `__suppress_context__=True` AND leaves the SSL error in `__context__`:

        [0] httpx.ConnectError    cause=ConnectError  suppress=True
        [1] httpcore.ConnectError cause=None  context=SSLCertVerificationError
                                  suppress=True   <-- honouring this returns None

    Honouring suppression therefore terminates the walk at precisely the node
    carrying the evidence, and the predicate returns False on every real
    certificate failure — the exact defect R-CERT was ruled to fix. That flag
    was absent from the evidence the ruling was made on; it is measured here.

    SF3's concern is still answered structurally, because it never depended on
    the suppression clause: a node with an explicit `__cause__` ignores its
    context entirely, so a stale SSL error sitting BESIDE a real cause is not
    matched. What is given up is only the `raise ... from None` case, where the
    worst outcome is a five-minute loud exit with a cert-named record.
    """
    if exc.__cause__ is not None:
        return exc.__cause__
    return exc.__context__


def is_cert_verify_fault(exc: BaseException) -> bool:
    """True when exc's effective-cause chain holds an ssl.SSLCertVerificationError.

    Certificate failure is transient-on-a-short-ceiling rather than permanent:
    a captive portal, an intercepting proxy and a renewal gap all produce the
    same client-side message, so exiting on sight would kill the worker on
    ordinary weather (spec brief §3.2 rule 2).

    EFFECTIVE-cause, not `__cause__` alone. The measured chain from a live
    handshake against a wrong-hostname certificate is:

        httpx.ConnectError                       suppress=True
          __cause__   = httpcore.ConnectError
              __cause__   = None                   suppress=True
              __context__ = ssl.SSLCertVerificationError

    The mechanism, traced in the pinned httpcore 1.0.9 rather than guessed at:
    `httpcore/_exceptions.py:14` wraps the ssl error EXPLICITLY
    (`raise to_exc(exc) from exc`), and then
    `httpcore/_async/connection_pool.py:256` re-raises that same object with
    `raise exc from None`, wiping `__cause__` back to None and setting
    `__suppress_context__` while `__context__` keeps the ssl error. So the SSL
    error survives only in `__context__`, and a `__cause__`-only walk returns
    False on every real certificate failure — measured, not theoretical.

    Following `__context__` cannot resurrect the concern that a stale SSL error
    from an earlier handler gets matched: a node with an explicit `__cause__`
    ignores its context, and a suppressed context is ignored outright. The
    remaining false-positive worst case is a five-minute loud exit carrying a
    cert-named record, which fails safe; the false negative it replaces was a
    total failure of the cert design.
    """
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))                       # cause chains can be cyclic
        if isinstance(cur, ssl.SSLCertVerificationError):
            return True
        cur = _effective_cause(cur)
    return False


def backoff_delay(n: int, *, base: float | None = None,
                  cap: float = BACKOFF_CAP_S) -> float:
    """Equal-jitter delay for the nth consecutive failure: d/2 + uniform(0, d/2).

    Equal jitter over full jitter deliberately: full jitter's range is [0, cap]
    and is not monotone in n, which the probe spec cannot assert cleanly, while
    equal jitter keeps a [d/2, d] bound (spec brief §3.2 rule 4).
    """
    if base is None:
        base = POLL_INTERVAL_S
    n = max(0, min(int(n), _BACKOFF_MAX_N))     # clamp BEFORE the shift
    envelope = min(cap, base * (2 ** n))
    return envelope / 2.0 + random.uniform(0.0, envelope / 2.0)


def classify_fault(exc: BaseException | None = None, *,
                   status: int | None = None) -> tuple[str, bool]:
    """Classify one fault. Returns (verdict, is_cert_verify).

    Deliberately does NOT return a phase. Phase attaches at the catch site
    (Cowork R4): deriving it from a type list is what left an HTTP 502 — the
    only status class ever observed on this leg — with no phase and no orphan
    record, because a status fault appears in no exception list.

    Total over arbitrary exceptions so it stays unit-testable and so a future
    httpx upgrade cannot introduce a class that falls into a branch nobody
    chose. The default is PERMANENT: an unknown fault stops the worker and is
    seen, rather than going on an infinite quiet retry.
    """
    if status is not None:
        if 200 <= status < 300:
            return NOT_AN_ERROR, False
        return (TRANSIENT if status in TRANSIENT_STATUSES else PERMANENT), False

    if exc is None:
        raise ValueError("classify_fault needs either exc or status")

    cert = is_cert_verify_fault(exc)
    if cert:
        return TRANSIENT, True                  # short ceiling, not an exit
    if isinstance(exc, PERMANENT_EXCEPTIONS):
        return PERMANENT, False
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return TRANSIENT, False
    return PERMANENT, False                     # the stated default


def log_event(event: str, **fields) -> None:
    """One structured record. Values ride as fields, never interpolated.

    The message template is the bare event name; everything dynamic goes in the
    JSON payload, per the global logging rule. Greppable by `event=<name>`.

    Every record carries a `timestamp`, added here rather than at each call
    site so no record can ship without one. Rule 6 requires it on the orphan
    record specifically — that is the line an operator reconciles a stranded
    job against — and `worker.log` itself is undated (the unit appends raw
    stdout), which is what made the build step 0 measurements need the systemd
    journal to recover any timing at all. A wall clock is right here: this is a
    log field for a human, not a duration measurement, so it does not touch the
    monotonic-clock rule the ceilings run on.
    """
    print(f"event={event} " + json.dumps(event_payload(**fields),
                                          default=str, sort_keys=True),
          file=sys.stderr, flush=True)


def event_payload(**fields) -> dict:
    """The enrichment every record gets, factored out so it is testable.

    Public rather than private on purpose: the probe suite records events by
    substituting log_event, so without a shared enrichment it would be
    asserting against fields the real logger adds and the stub does not — a
    probe passing on a build that never ships the field. It calls this.
    """
    return {"timestamp": time.time(), **fields}


class FaultTracker:
    """The four pieces of streak state, and the two ceilings over them.

    A ceiling measures UNINTERRUPTED FAILURE-STREAK DURATION (Cowork R3): it
    anchors at the first fault of the streak and is CLEARED by a success, so
    time spent inside a successfully-running job can never contribute. The
    rejected reading — "time since the last success" — would exit the worker on
    the first fault after any long sweep, which is the workload this unit exists
    to unblock.

    State is per process and shared across all three request legs: a worker
    whose /online failed and whose polls keep failing is in ONE outage.
    """

    def __init__(self, *, clock=time.monotonic,
                 fault_ceiling_s: float | None = None,
                 cert_ceiling_s: float | None = None) -> None:
        # Monotonic by injection so probes can drive it. On Linux this is
        # CLOCK_MONOTONIC, which does not advance while the machine is
        # suspended — a laptop closed mid-outage resumes where it left off.
        self._clock = clock
        self.fault_ceiling_s = FAULT_CEILING_S if fault_ceiling_s is None else fault_ceiling_s
        self.cert_ceiling_s  = CERT_CEILING_S  if cert_ceiling_s  is None else cert_ceiling_s
        self.consecutive = 0        # 1: failure count (also the backoff level)
        self.anchor      = None     # 2: general ceiling anchor
        self.cert_anchor = None     # 3: cert ceiling anchor
        self._level      = None     # last logged backoff level, for log cadence

    def record_success(self) -> dict | None:
        """Clear all four pieces. Returns the recovery record, or None."""
        if self.consecutive == 0:
            return None
        # `is not None`, NOT truthiness: an anchor of exactly 0.0 is a
        # legitimate monotonic reading, and `if self.anchor` would silently
        # report a zero-length outage for it.
        rec = {"streak_length": self.consecutive,
               "outage_seconds": (round(self._clock() - self.anchor, 3)
                                  if self.anchor is not None else 0.0)}
        self.consecutive = 0
        self.anchor = None
        self.cert_anchor = None
        self._level = None
        return rec

    def record_failure(self, *, is_cert: bool) -> None:
        now = self._clock()
        self.consecutive += 1
        if self.anchor is None:
            self.anchor = now
        if is_cert:
            if self.cert_anchor is None:    # anchors at the first CERT fault,
                self.cert_anchor = now      # which may be mid-streak
        else:
            self.cert_anchor = None         # measures CONTINUOUS cert failure

    def ceiling_hit(self) -> str | None:
        """Which ceiling fired, if any. Whichever is reached first wins."""
        now = self._clock()
        if self.cert_anchor is not None and now - self.cert_anchor >= self.cert_ceiling_s:
            return "cert_verify"
        if self.anchor is not None and now - self.anchor >= self.fault_ceiling_s:
            return "general"
        return None

    def should_log_full(self, *, base: float | None = None,
                        cap: float = BACKOFF_CAP_S) -> bool:
        """First failure of a streak, and each backoff-level change, only.

        The level is the CAPPED envelope, not the raw failure count: once the
        backoff saturates at the cap the level stops changing, so a long outage
        writes a handful of records and then goes quiet. Keying on the raw count
        would log every failure forever and regrow the 7.9 MB log this unit is
        partly about.
        """
        if base is None:
            base = POLL_INTERVAL_S
        n = max(0, min(int(self.consecutive) - 1, _BACKOFF_MAX_N))
        level = min(cap, base * (2 ** n))
        if self._level is None or level != self._level:
            self._level = level
            return True
        return False


load_dotenv(os.path.expanduser("~/.research-worker.env"))

API_KEY    = os.environ["RESEARCH_WORKER_API_KEY"]
SERVER_URL = os.environ["RESEARCH_SERVER_URL"]
WORKER_ID  = os.environ.get("RESEARCH_WORKER_ID", "laptop-wsl2")

POLL_INTERVAL_S      = 2.0
REQUEST_TIMEOUT_S    = 30.0   # the AsyncClient timeout; P15 pins it + BACKOFF_CAP_S <= 90

# Ceilings, on the MONOTONIC clock, measuring uninterrupted failure-streak
# duration (Cowork R3). Overridable for the deploy gate's live demonstration.
#
# These sit BELOW load_dotenv, unlike the exception tuples above it. The tuples
# read no environment at all, so they can live above it and stay importable by
# an offline probe suite. These two DO read the environment, so placing them
# above load_dotenv would bind them to their defaults before ~/.research-worker.env
# was ever parsed — silently ignoring an override written in the same file every
# other worker dial lives in. systemd's EnvironmentFile= and the gate's drop-in
# both set real process env vars and would have masked it; running worker.py
# directly to test an override would not.
FAULT_CEILING_S = float(os.environ.get("RESEARCH_WORKER_FAULT_CEILING_S", 3600.0))
CERT_CEILING_S  = float(os.environ.get("RESEARCH_WORKER_CERT_CEILING_S", 300.0))

# Lane W1: how many jobs this worker runs at once. Only fetch_dataset jobs share the slots; every other
# kind waits until all slots are free and runs alone (worker.py: SLOTTED_KINDS). Default 1 is the
# one-job-at-a-time worker this was before W1. Read below load_dotenv for the same reason as the
# ceilings above.
def _worker_slots(raw: str) -> int:
    try:
        slots = int(raw)
    except ValueError:
        raise ValueError(f"RESEARCH_WORKER_SLOTS must be a whole number of at least 1, got {raw!r}") from None
    if slots < 1:
        raise ValueError(f"RESEARCH_WORKER_SLOTS must be a whole number of at least 1, got {raw!r}")
    return slots


WORKER_SLOTS = _worker_slots(os.environ.get("RESEARCH_WORKER_SLOTS", "1"))

# Stranded-job cleaner step 1 (spec brief §3.1, §5.4 dial 1). The cadence every job kind now
# heartbeats at, on the report_heartbeat helper below. An operational dial under DECISIONS #204,
# not a provenance parameter: it can only change how fast a real death becomes visible, never a
# number a backtest produces, so it carries no parameter_provenance.md entry.
#
# Source: step 0a's measured distribution of achievable heartbeat gaps. The three kinds this step
# newly heartbeats came back at a 1.006 s / 1.005 s median against a 1.0 s candidate, worst drift
# +60 ms, so placement adds essentially nothing to whatever cadence is asked for. The two existing
# cancel watchers already sit at CANCEL_POLL_S = 1.0, but that cadence is bought for cancellation
# polling, not for liveness, and does not transfer.
#
# Mechanism: this cadence bounds how stale a healthy job can look, so it sets the floor under the
# stale threshold the sweep will use (a later step). Against the committed p99 network outage of
# 7 minutes 31 seconds, every candidate between 1 s and 30 s moves that floor by under one percent,
# so the cadence is effectively free on detection latency and is chosen on write cost instead.
#
# Sensitivity: downward, each heartbeat is a full BEGIN IMMEDIATE transaction on a single-writer
# database, and this step takes the heartbeating kinds from three to six -- a tenfold write-lock
# rate at 1 s buys no detection benefit and lands immediately before the M5 Layer 3 volume increase.
# Upward, a job shorter than one tick delivers no heartbeat at all, which is harmless while the
# stale threshold is minutes and stops being harmless only if that threshold is ever shortened.
# Both directions are reversible and neither destroys work.
#
# Bounds: above roughly 1 s, below which it is pointless traffic, and well below the stale
# threshold's floor (above 7 minutes 31 seconds). 10 s sits an order of magnitude inside both ends.
HEARTBEAT_CADENCE_S = 10.0
HEADERS              = {"X-Worker-API-Key": API_KEY}


async def report_log(job_id: int, line: str, http: httpx.AsyncClient,
                     level: str = "info") -> None:
    await http.post(
        f"{SERVER_URL}/api/research/worker/jobs/{job_id}/log",
        json={"level": level, "line": line},
        headers=HEADERS,
    )


async def report_heartbeat(job_id: int, http: httpx.AsyncClient) -> bool:
    """Heartbeat and return whether cancellation was requested for this job (brief I, EI-2).

    The worker has no DB access, so the cancel signal rides back on the heartbeat response.
    run_backtest polls this at the L7 1-second cadence during a backtest and hard-kills its
    container when the flag flips true.
    """
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/jobs/{job_id}/heartbeat",
        json={"worker_id": WORKER_ID},
        headers=HEADERS,
    )
    try:
        return bool(r.json().get("cancellation_requested", False))
    except (ValueError, AttributeError):  # non-JSON / unexpected body: treat as no cancel
        return False


async def heartbeat_loop(job_id: int, kind: str, http: httpx.AsyncClient) -> None:
    """Post a heartbeat every HEARTBEAT_CADENCE_S for as long as the caller's job is running.

    ONE copy, consumed by all four job kinds that need a concurrent heartbeat. It first shipped as
    four near-identical private copies, one per handler, justified by two of those modules being
    deliberately import-light so a plain-interpreter probe can import them. The code-quality gate
    pointed out that the justification only covered two: `fetch_dataset` and `fake` both import httpx
    at module level and are not import-light at all, so half the duplication had no argument behind
    it. Import-lightness is preserved for the two that need it the same way they already handle
    `report_heartbeat` -- by importing this at function scope inside `run()` rather than at module
    level -- so factoring costs those modules nothing.

    The first heartbeat lands one full cadence AFTER the task starts, not immediately, because
    `worker_poll` already wrote `last_heartbeat` at claim time. A job shorter than one cadence
    therefore posts nothing and is correct: it is terminal long before any stale threshold, and its
    claim timestamp is the liveness proof for its whole life.

    `cancellation_requested` from the response is deliberately ignored. These kinds have no cancel
    path -- `request_cancel` answers `unsupported` for every one of them -- so the next reader must
    not take the return type as a contract to honour.

    A delivery failure is caught HERE and logged, never allowed to escape: this task runs beside real
    work, and a heartbeat that cannot be posted must not fail the job it exists to protect.
    `httpx.HTTPError` is the root of every exception httpx itself raises from a POST, verified
    against the pinned 0.28.1 rather than assumed, so this is a specific named type and not a
    catch-all.
    """
    while True:
        await asyncio.sleep(HEARTBEAT_CADENCE_S)
        try:
            await report_heartbeat(job_id, http)
        except httpx.HTTPError as exc:
            log_event("heartbeat_post_failed", job_id=job_id, kind=kind,
                      exception=f"{type(exc).__module__}.{type(exc).__name__}", message=str(exc))


async def stop_heartbeat(task: "asyncio.Task | None", job_id: int, kind: str) -> None:
    """Stop a heartbeat task and collect its outcome WITHOUT re-raising. Safe on None.

    Called from each handler's `finally`, so it must not raise: a raise here replaces whatever
    exception sent us into the `finally` and, in one handler, would also skip the scratch-directory
    cleanup that follows it.

    The re-raise it prevents is reachable rather than theoretical. A plain `await task` re-raises
    anything the task died of that is not `CancelledError`, and a POST on an already-closed
    `AsyncClient` raises `RuntimeError("Cannot send a request, as the client has been closed.")`,
    which is NOT an `httpx.HTTPError` and so escapes the loop's own handler. Measured against
    httpx 0.28.1. That would fail a job whose work had already succeeded -- the one thing this
    heartbeat must never do. `gather(..., return_exceptions=True)` hands the exception back as a
    value instead of throwing it.

    A task that died early stopped protecting its job, so it is LOGGED rather than swallowed.
    `CancelledError` is the normal path and is not an anomaly.

    Accepts None so a caller whose task is created partway through its own `try` can call this
    unconditionally.
    """
    if task is None:
        return
    task.cancel()
    outcome = (await asyncio.gather(task, return_exceptions=True))[0]
    if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
        log_event("heartbeat_task_died", job_id=job_id, kind=kind,
                  exception=f"{type(outcome).__module__}.{type(outcome).__name__}",
                  message=str(outcome))


async def report_job_completed(job_id: int, result: dict,
                               http: httpx.AsyncClient) -> None:
    await http.post(
        f"{SERVER_URL}/api/research/worker/jobs/{job_id}/complete",
        json={"status": "succeeded", "result": result},
        headers=HEADERS,
    )


async def report_job_failed(job_id: int, error: str,
                            http: httpx.AsyncClient) -> None:
    await http.post(
        f"{SERVER_URL}/api/research/worker/jobs/{job_id}/complete",
        json={"status": "failed", "error": error},
        headers=HEADERS,
    )


async def report_full_metrics_cache(run_id: int, variation_id: int, ticker: str, formula_version: str,
                                    payload: dict, http: httpx.AsyncClient) -> None:
    """POST a worker-computed full-metrics payload to the backend cache (M5 Track C, C-metrics §2.2).

    The backend INSERT-OR-REPLACEs it under (run_id, variation_id, ticker, formula_version). raise_for_status
    so a failed cache write fails the compute job (the metrics are NOT silently lost; the next read re-dispatches)."""
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/runs/{run_id}/full-metrics-cache",
        json={"variation_id": variation_id, "ticker": ticker,
              "formula_version": formula_version, "payload": payload},
        headers=HEADERS,
    )
    r.raise_for_status()


async def report_dataset_ready(dataset_id: int, result: dict,
                               http: httpx.AsyncClient) -> None:
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/datasets/{dataset_id}/result",
        json=result,
        headers=HEADERS,
    )
    r.raise_for_status()


async def report_dataset_failed(dataset_id: int, error: str,
                                http: httpx.AsyncClient) -> None:
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/datasets/{dataset_id}/failed",
        json={"error": error},
        headers=HEADERS,
    )
    r.raise_for_status()


async def report_dataset_hard_deleted(dataset_id: int,
                                      http: httpx.AsyncClient) -> None:
    """Tell the server the parquet is gone; it writes the cleaned terminal state (B.5)."""
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/datasets/{dataset_id}/hard_deleted",
        json={},
        headers=HEADERS,
    )
    r.raise_for_status()


async def report_run_artifacts(run_id: int, files: dict[str, Path],
                               http: httpx.AsyncClient) -> None:
    """Upload a succeeded run's artifacts to the server (brief K.0; C2.5 adds meta.json).

    `files` maps the stored filename (e.g. 'equity.parquet', 'meta.json') to its host path —
    generic over the dict, so the caller decides what to send. The parquets + meta.json are
    sub-MB at M3, so each is read fully and sent as multipart, alongside a JSON manifest of
    per-file sha256 the server verifies on write. This MUST succeed before /result is allowed
    to flip the run to 'succeeded' (the K.0 invariant: no succeeded-without-bytes).
    Raises on a non-2xx so the caller fails the run rather than reporting success.
    (A future 5 GB portfolio.vbt pin would stream instead of read_bytes; the small artifacts
    here do not need streaming on the worker side.)
    """
    manifest: dict[str, str] = {}
    multipart: list = []
    for name, path in files.items():
        data = Path(path).read_bytes()  # whole file: parquets are sub-MB at M3
        manifest[name] = hashlib.sha256(data).hexdigest()
        multipart.append(("files", (name, data, "application/octet-stream")))
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/runs/{run_id}/artifacts",
        data={"manifest": json.dumps(manifest)},
        files=multipart,
        headers=HEADERS,
    )
    r.raise_for_status()


async def report_dataset_preview(dataset_id: int, parquet_path: Path,
                                 http: httpx.AsyncClient) -> None:
    """Upload a fetched dataset's full parquet to the server as its chart preview (Brief B, R9).

    Mirrors report_run_artifacts but single-file: read the whole parquet (a price parquet is
    sub-MB to a few MB) and send it multipart with a one-entry {name: sha256} manifest the server
    verifies on write. Unlike the run-artifact upload, the CALLER (fetch_dataset) treats this as
    BEST-EFFORT — it swallows the raise so a failed upload leaves the read endpoint's honest
    available:false state instead of failing an already-ready dataset. This helper still raises on
    a non-2xx (the server rejecting a corrupt upload) and lets read_bytes' OSError propagate; the
    caller's except handles both.
    """
    data = Path(parquet_path).read_bytes()  # whole file: a price parquet is sub-MB to a few MB
    manifest = {"preview.parquet": hashlib.sha256(data).hexdigest()}
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/datasets/{dataset_id}/preview",
        data={"manifest": json.dumps(manifest)},
        files=[("files", ("preview.parquet", data, "application/octet-stream"))],
        headers=HEADERS,
    )
    r.raise_for_status()


async def report_event_materialize(event_type: str, ciks: list[int],
                                   knowledge_from: int, knowledge_to: int,
                                   include_status: list[str],
                                   http: httpx.AsyncClient,
                                   universe_id: int | None = None) -> tuple[bytes, dict]:
    """Call the M2.5 brief K materializer (§6.10) and return (parquet_bytes, headers).

    POSTs one (event_type, cik-universe, window) slice; the server queries
    events.db and streams back a denormalized per-(event,cik) parquet. Returns
    the raw bytes plus the response headers (X-Event-Count / X-Content-SHA256 /
    X-Source-Series) so the caller writes the parquet + a meta.json sidecar.
    Raises on a non-2xx so the dispatch fails honestly rather than mounting a
    bad slice (same discipline as report_run_artifacts).

    `universe_id` (M3.5 brief I, the worker half of EL65 / DECISIONS #54) binds the
    resolved universe so the server stamps each row's per-event membership_valid flag
    (§3.2). It is added to the body only when set, so the M2.5 brief-K/N callers that
    pass no universe_id send the byte-identical pre-M3.5 body and get NULL membership.
    """
    body = {
        "event_type": event_type,
        "ciks": ciks,
        "knowledge_from": knowledge_from,
        "knowledge_to": knowledge_to,
        "include_status": include_status,
    }
    if universe_id is not None:                # absent (not null) for M2.5 callers -> NULL membership_valid
        body["universe_id"] = universe_id
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/events/materialize",
        json=body,
        headers=HEADERS,
    )
    r.raise_for_status()
    return r.content, dict(r.headers)


async def report_fundamentals_materialize(ciks: list[int], knowledge_from: int, knowledge_to: int,
                                          concepts: list[str], http: httpx.AsyncClient) -> tuple[bytes, dict]:
    """Call the M3.75 FUND-2 fundamentals materializer (§6.2) and return (parquet_bytes, headers).

    POSTs one (ciks, knowledge-window, concepts) request; the server queries events.db and streams
    back the denormalized fact-table parquet slice (every kept vintage in the window). Returns the
    raw bytes plus the response headers (X-Fact-Count / X-Content-SHA256) so the caller writes the
    parquet + a meta.json sidecar (the re-derivability sidecar, §6.4). `concepts` empty -> the server
    materializes all four core concepts (§6.1). Raises on a non-2xx so the dispatch fails honestly
    rather than mounting a bad slice (the report_event_materialize discipline).
    """
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/fundamentals/materialize",
        json={
            "ciks": ciks,
            "knowledge_from": knowledge_from,
            "knowledge_to": knowledge_to,
            "concepts": concepts,                  # empty list -> all four core concepts (§6.1)
        },
        headers=HEADERS,
    )
    r.raise_for_status()
    return r.content, dict(r.headers)


async def resolve_universe(universe_id: int, knowledge_from: int, knowledge_to: int,
                           http: httpx.AsyncClient,
                           fundamental_screen: dict | None = None,
                           members_as_of: int | None = None) -> list[int]:
    """Resolve a universe to its UNION-over-window CIK list (M3.5 brief I, §4 / EL63).

    POSTs the §4 server endpoint (the worker is on the laptop and cannot read events.db,
    so the universe-to-CIK resolution is a server step the worker calls before
    materializing). Returns the union membership read over [knowledge_from, knowledge_to)
    (DECISIONS #54: every name that was a member at any point in the window, NOT just at
    window-end, which is the survivorship-bias fix). An empty universe returns [] (a valid
    response the §8 wiring short-circuits, never sent on to the materializer). Raises on a
    non-2xx so the dispatch fails honestly with no fallback (same discipline as
    report_event_materialize).

    M3.75 FUND-4 §9: when the run's payload carries a `fundamental_screen` (the strategy declared
    one), it is passed through so the server post-filters the resolved union to the companies whose
    as-known-on-D fundamental satisfies the screen (the events.db-local calendar-keyed read, §9). It
    is OMITTED from the POST body when None (every existing run), so the request — and the resolved
    list — is byte-unchanged (NFR-6).

    DL1 (DECISIONS #455): `members_as_of` (unix seconds, pinned by the run's payload for a price universe)
    asks the server for the list as it stood then. It is OMITTED from the body when None. A server that does
    not echo it back is an old server that ignored it, which would silently resolve today's list: that raises.
    """
    body = {
        "universe_id": universe_id,
        "knowledge_from": knowledge_from,
        "knowledge_to": knowledge_to,
    }
    if fundamental_screen is not None:  # FUND-4 §9: present iff the strategy declared a screen
        body["fundamental_screen"] = fundamental_screen
    if members_as_of is not None:  # DL1: present iff the run pinned a date for a price universe
        body["members_as_of"] = members_as_of
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/universes/resolve",
        json=body,
        headers=HEADERS,
    )
    r.raise_for_status()
    data = r.json()
    if members_as_of is not None and data.get("members_as_of") != members_as_of:
        raise httpx.HTTPError(
            f"universe {universe_id}: the server did not echo members_as_of={members_as_of} "
            f"(got {data.get('members_as_of')!r}); it may be too old to read the member list as of a date"
        )
    return data["ciks"]


async def resolve_price_slices(symbols: list[str], http: httpx.AsyncClient) -> dict:
    """Resolve symbols to their ready daily price file_paths (M3.5 price-provisioning brief, EL70).

    POSTs the price-brief §3.1 server endpoint. The worker is on the laptop and cannot read
    research.db, so the symbol->price-dataset lookup is a server step the worker calls before
    provisioning. Returns {"resolved": {symbol: container_path}, "unresolved": [symbol, ...]}. Bytes
    never move — the container-relative file_path is the parquet the worker already holds locally.
    Daily-only (the locked M3.5 granularity), so no timeframe is sent. Raises on a non-2xx so the run
    fails honestly with no fallback (same discipline as resolve_universe).
    """
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/price-slices/resolve",
        json={"symbols": symbols},
        headers=HEADERS,
    )
    r.raise_for_status()
    return r.json()


async def report_run_result(run_id: int, result: dict,
                            http: httpx.AsyncClient) -> None:
    """Report a succeeded run (M3 brief D). The backend (brief E) maps it onto research_runs.

    Payload: status, run_id, artifacts (six container-relative paths), stats (eight
    research_run_stats keys), environment (four fields copied verbatim from meta.json).
    The worker authors none of these from vbt — the container did (DECISIONS #20).
    """
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/runs/{run_id}/result",
        json=result,
        headers=HEADERS,
    )
    r.raise_for_status()


async def report_run_failed(run_id: int, error: str,
                            http: httpx.AsyncClient) -> None:
    """Report a failed run (M3 brief D). The backend (brief E) flips the run to failed."""
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/runs/{run_id}/failed",
        json={"error": error},
        headers=HEADERS,
    )
    r.raise_for_status()


async def fail_run_and_job(run_id: int, job_id: int, error: str,
                           http: httpx.AsyncClient) -> None:
    """Fail BOTH a run and its job terminally — the shared stuck-run guard (M5 UU scale-defect 1.1).

    The worker loop's backstop (worker.handle_job) only fails the JOB when a handler raises, so an
    unexpected fault — the readline-limit ValueError of scale-defect 1, or a result-POST 409 of
    scale-defect 2 — used to leave the RUN stuck 'pending'/'running'. This helper fails the run leg
    first (the one the loop's job-only path omits), then the job leg. report_run_failed raises on a
    non-2xx, so the run leg is attempted first: a hiccup there surfaces loudly rather than being hidden
    behind a finalized job. Reused by both run handlers' top-level safety nets and the loop backstop.
    """
    await report_run_failed(run_id, error, http)   # the leg worker.handle_job's job-only path omits
    await report_job_failed(job_id, error, http)   # then finalize the job


async def report_run_cancelled(run_id: int, http: httpx.AsyncClient,
                               status: str = "cancelled") -> None:
    """Report a cancelled run after a hard-kill (M3 brief I). Backend NULLs paths + finalizes.

    status is sent in the body (default 'cancelled') so M5/M6 'cancelled_partial' reuse this.
    """
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/runs/{run_id}/cancelled",
        json={"status": status},
        headers=HEADERS,
    )
    r.raise_for_status()


async def report_job_cancelled(job_id: int, http: httpx.AsyncClient) -> None:
    """Finalize the job as cancelled (M3 brief I) — same /complete channel as succeeded/failed."""
    await http.post(
        f"{SERVER_URL}/api/research/worker/jobs/{job_id}/complete",
        json={"status": "cancelled"},
        headers=HEADERS,
    )


async def resolve_committed_cells(run_id: int, http: httpx.AsyncClient) -> list:
    """Read this sweep's already-committed cells so a resume does not recompute them (M5 1B §6.2).

    Returns ``[[variation_id, ticker], ...]`` — the cells already in ``research_run_cells`` for the run. On
    a fresh sweep this is empty (the entrypoint then runs the whole grid). The backend idempotence check
    (the research_run_cells PK gate) is the correctness backstop regardless of this read; this is the
    compute-efficiency optimisation (the container skips recomputing committed cells). Raises on a non-2xx
    so a resume fails honestly rather than recomputing everything as a hidden fallback.
    """
    r = await http.get(
        f"{SERVER_URL}/api/research/worker/runs/{run_id}/committed-cells",
        headers=HEADERS,
    )
    r.raise_for_status()
    return r.json()["cells"]


async def report_sweep_chunk(run_id: int, cells: list, http: httpx.AsyncClient) -> dict:
    """POST one sweep chunk's cells; the backend writes them atomically + idempotently (M5 1B §6.1).

    The per-chunk analogue of report_run_result: each cell carries variation_id, ticker, param_json,
    dataset_sha256, the eight metrics, and the nine funnel counts + zero_reason. The backend
    (complete_sweep_chunk) opens ONE transaction, skips any cell already in research_run_cells (the
    exactly-once gate), writes the trial+cell+funnel+variation for the rest, and advances progress_json.
    Returns the backend's ``{"written": n, "skipped": m, "done": d, "total": t}``. Raises on a non-2xx so
    a chunk that did not commit fails the run (the worker re-runs it on resume; the chunk is the atomic
    unit, §6).
    """
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/runs/{run_id}/sweep-chunk",
        json={"cells": cells},
        headers=HEADERS,
    )
    r.raise_for_status()
    return r.json()


async def report_cell_refusals(run_id: int, refusals: list, http: httpx.AsyncClient) -> dict:
    """Lane D1: POST refused sweep cells (sizing refused one (combo, ticker); the sweep carried on) so the
    backend records each in research_run_cell_refusals. Returns {"written", "skipped"}. Raises on a non-2xx
    so a refusal that was not recorded fails the run rather than vanishing."""
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/runs/{run_id}/cell-refusals",
        json={"refusals": refusals},
        headers=HEADERS,
    )
    r.raise_for_status()
    return r.json()


async def report_sweep_result(run_id: int, annualization: dict | None, environment: dict,
                              total_cells: int, http: httpx.AsyncClient, *, bars_observed: int | None = None) -> None:
    """Finalize a succeeded sweep run from the entrypoint's sweep-done line (M5 1B §7, the complete_sweep call).

    `bars_observed` (lane GV2-8b, DECISIONS #459 item 2) is the count of bars a theory search or walk-forward run read, for runs that
    write no funnel rows; it is sent in the body only when given, and the server records it for the run's draw.

    A sweep writes no per-run artifacts and no research_run_stats row (a multi-cell sweep has no single
    run-level metric, §7/§14.5); the cube cells the chunks wrote are the result. This flips the run to
    'succeeded', records the run-level annualization (one factor for a single-asset-class sweep, NULL for a
    mixed one, §15.1), and stamps the environment. Raises on a non-2xx so the run is not left unfinalized.
    """
    body = {"annualization": annualization, "environment": environment, "total_cells": total_cells}
    if bars_observed is not None:
        body["bars_observed"] = bars_observed
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/runs/{run_id}/sweep-result",
        json=body,
        headers=HEADERS,
    )
    r.raise_for_status()


# ---------------------------------------------------------------------------------------------
# Set 16b, lane WD (#423): the worker's front door. Three calls that used to build their own URL
# inline in run_generator.py / run_retest.py (two of them with NO worker key header at all) now
# live here as named functions, beside every other report_*/resolve_* helper in this file, so
# "one file holds every worker URL" (AC-5) is true by construction rather than by convention. Each
# targets a route under /api/research/worker/ (the ONLY prefix production's Caddy lets through
# without human basic-auth, dashboard/deploy/Caddyfile's own @research_worker matcher), behind
# require_worker_api_key, and each sends HEADERS the same way every sibling helper above does.
# ---------------------------------------------------------------------------------------------


async def fetch_theory_content(theory_sha256: str, http: httpx.AsyncClient) -> str:
    """The worker's own twin of the Generator page's GET /api/research/generator/theories/{sha}
    (AC-1): the same saved theory text, read through a worker-scoped route so Caddy's basic-auth
    front door lets the worker through at all (the browser route sits outside /api/research/worker/
    and a laptop worker calling it directly gets Caddy's own 401, never the backend). Raises on any
    non-2xx (an unknown sha256, or the unknown-`/api/`-path 404 finding 1 fixed) so the caller fails
    the job honestly instead of reading a "content" key off an error body that was never there --
    the exact KeyError this route replaces (set 16b finding 1, docs/units/WD/criteria.md).
    """
    r = await http.get(
        f"{SERVER_URL}/api/research/worker/generator/theories/{theory_sha256}",
        headers=HEADERS,
    )
    r.raise_for_status()
    return r.json()["content"]


async def report_generator_facts(job_id: int, facts_payload: dict, http: httpx.AsyncClient) -> None:
    """Relay compile_facts.py's own compiled-facts JSON to the backend's worker-scoped facts route
    (AC-2), which runs the host-side generation loop and marks the job terminal itself. Moved here
    from a bare URL built inline in run_generator.py (AC-5); behaviour is otherwise unchanged.
    Raises on a non-2xx so a failed relay fails the job rather than losing a finished compile step.
    """
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/generator/jobs/{job_id}/facts",
        json=facts_payload,
        headers=HEADERS,
    )
    r.raise_for_status()


async def report_retest_result(job_id: int, body: dict, http: httpx.AsyncClient) -> None:
    """Relay a finished retest's own result to the backend's worker-scoped result route (AC-3),
    which records it once, keyed on the retest job job_id names (the route itself now confirms
    that job_id names an existing run_retest job whose own dispatched run_id matches body's
    run_id, refusing by name before any write when it does not). Moved here from a bare URL built
    inline in run_retest.py (AC-5), same headers=HEADERS the inline call already sent. Raises on a
    non-2xx so a failed relay fails the job rather than silently losing a finished retest.
    """
    r = await http.post(
        f"{SERVER_URL}/api/research/worker/retests/{job_id}/result",
        json=body,
        headers=HEADERS,
    )
    r.raise_for_status()


async def fetch_cscv_split_count(http: httpx.AsyncClient) -> int:
    """Lane C5 (set 16c, #427, plan amendment 4): the sweep container has no database
    (research_lab/CLAUDE.md, "How a job runs": "There is no database access anywhere in this
    area"), so check 5's own frozen `cscv_split_count` (S, AFML section 11.6) reaches it through
    this ONE worker-scoped GET, matching the WD idiom above ("one file holds every worker URL").

    Raises on a non-2xx (an unfrozen row, or the route unreachable), the SAME contract every
    sibling helper in this file keeps. The CALLER (run_sweep.py's own `run()`) decides what a
    failure here means: never "fail the whole sweep", always "S is unavailable this sweep, skip
    the cscv artifact" (plan amendment 4's own words), because a sweep's other results are real
    and valid whether or not this one optional artifact can be built.
    """
    r = await http.get(
        f"{SERVER_URL}/api/research/worker/judge/cscv-split-count",
        headers=HEADERS,
    )
    r.raise_for_status()
    return int(r.json()["cscv_split_count"])
