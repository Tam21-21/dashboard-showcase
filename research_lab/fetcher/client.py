"""The async EODHD HTTP client: rate limiting, daily-cap accounting, retry taxonomy,
and token redaction (brief §3, §8).

CostLedger and RateLimiter are separated as plain classes so the probe can assert the
hard-stop and the bucket without a network. fetch_json applies the brief's retry rules:
422 is a programming-guard (no retry), 401/403 fails the whole run, transient 5xx/429/
timeout is bounded-backoff retried, an empty 200 is a real coverage gap (returned, not raised).
"""

import asyncio  # async sleeps for backoff + the rate-limiter wait
import os  # read the API key from the environment
import random  # jitter on the backoff so retries do not synchronise
import time  # monotonic clock for the token bucket

import httpx  # the async HTTP client
from dotenv import load_dotenv  # load EODHD_API_KEY from ~/.research-worker.env

from .config import (DEFAULT_DAILY_CAP, DEFAULT_DAILY_MARGIN, DEFAULT_REQ_PER_MIN,
                     DEFAULT_RETRY_MAX, USER_URL, env_file)


class EodhdAuthError(RuntimeError):
    """401/403 — a bad/again key. Fails the whole run loud (no point continuing)."""


class EodhdCapError(RuntimeError):
    """422 — a span exceeded the published cap (e.g. a 1-min window > 120 days).

    This is a programming-guard assertion (the window generator should never emit one),
    NOT a transient condition, so it is never retried (brief M2.4).
    """


class EodhdTransientError(RuntimeError):
    """A transient failure (timeout, connection error, 5xx, 429) that exhausted retries."""


class EodhdShapeError(RuntimeError):
    """A non-JSON body or an unexpected non-retryable status (other 4xx)."""


class CapReached(RuntimeError):
    """Control signal: the daily call cap would be exceeded. The pool stops and resumes later.

    Not an error condition — the manifest holds the un-issued work as 'pending' and the next
    launch continues (brief §8). Raised before a request is issued, never after.
    """


def read_api_key() -> str:
    """Read EODHD_API_KEY from ~/.research-worker.env or fail loud (never fabricate)."""
    load_dotenv(env_file())  # populate the environment from the dotenv file
    key = os.getenv("EODHD_API_KEY")  # the only source of the secret
    if not key:  # unset/empty -> stop before spending any quota
        raise EodhdAuthError(
            "EODHD_API_KEY not set. Add EODHD_API_KEY=<key> to ~/.research-worker.env (mode 600)."
        )
    return key  # the secret, never logged


def _redact(params: dict) -> dict:
    """Return a copy of params with the API token masked, for safe error messages."""
    safe = dict(params)  # shallow copy so we never mutate the caller's params
    if "api_token" in safe:  # mask the secret if present
        safe["api_token"] = "<redacted>"  # never echo the real key
    return safe  # safe to log


class CostLedger:
    """Tracks daily call consumption and enforces the hard stop below the cap (brief §8).

    Seeded FROM the live /user counter at launch (not 0), because /user is a single global
    daily number across all key usage. The effective ceiling is cap - margin, with the margin
    sized to exceed one reconcile-interval burst so the pool can never overshoot the real cap.
    """

    def __init__(self, daily_cap: int = DEFAULT_DAILY_CAP, margin: int = DEFAULT_DAILY_MARGIN):
        self.cap = daily_cap  # the real daily cap (min of published and dailyRateLimit)
        self.margin = margin  # safety headroom below the cap
        self.used = 0  # calls consumed today (seeded at launch)

    @property
    def effective_cap(self) -> int:
        """The ceiling the pool stops at — strictly below the real cap by the margin."""
        return self.cap - self.margin

    def seed(self, used_now: int) -> None:
        """Set the running counter from the live /user apiRequests value at launch/reconcile."""
        self.used = used_now  # adopt the global counter as the authority

    def can_spend(self, cost: int) -> bool:
        """True iff charging `cost` would keep us at or below the effective cap."""
        return self.used + cost <= self.effective_cap  # the hard-stop predicate

    def charge(self, cost: int) -> None:
        """Account `cost` calls against the running counter (after issuing a request)."""
        self.used += cost  # local accounting between reconciles


class RateLimiter:
    """A simple async token bucket enforcing <= req_per_min requests/minute (brief §8).

    Latency (5-20 s/request) means this rarely binds, but it is the guard against a burst
    exceeding the published 1,000/min. Refills continuously; `acquire` waits when empty.
    """

    def __init__(self, req_per_min: int = DEFAULT_REQ_PER_MIN):
        self.rate = req_per_min / 60.0  # tokens added per second
        self.capacity = float(req_per_min)  # full-minute burst capacity
        self.tokens = float(req_per_min)  # start full
        self.updated = time.monotonic()  # last refill time
        self._lock = asyncio.Lock()  # serialise token math across coroutines

    async def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:  # loop until a token is granted
            async with self._lock:  # one coroutine touches the bucket at a time
                now = time.monotonic()  # current time
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)  # refill
                self.updated = now  # advance the clock
                if self.tokens >= 1.0:  # a token is available
                    self.tokens -= 1.0  # consume it
                    return  # proceed with the request
                deficit = (1.0 - self.tokens) / self.rate  # seconds until one token refills
            await asyncio.sleep(deficit)  # wait outside the lock, then re-check


def _backoff_seconds(attempt: int, base: float = 2.0, ceiling: float = 60.0) -> float:
    """Exponential backoff with jitter for a transient retry (attempt starts at 1)."""
    raw = min(ceiling, base * (2 ** (attempt - 1)))  # exponential, capped
    return raw * (0.5 + random.random() / 2.0)  # 50-100% jitter to desynchronise retries


async def fetch_json(http: httpx.AsyncClient, url: str, params: dict, ctx: str,
                     timeout: float, retry_max: int = DEFAULT_RETRY_MAX):
    """Issue one GET with the brief's retry taxonomy; return parsed JSON (may be an empty list).

    ctx is a short label (ticker + endpoint) for error context only. The token in params is
    redacted in every error string. A 200 returns the parsed JSON (an empty list is a valid
    coverage gap the caller handles). 422 -> EodhdCapError (no retry). 401/403 -> EodhdAuthError.
    429/5xx/timeout/transport -> bounded backoff retry then EodhdTransientError. Other 4xx /
    non-JSON -> EodhdShapeError.
    """
    attempt = 0  # retry counter
    while True:  # retry loop for transient failures
        attempt += 1  # count this attempt
        try:
            resp = await http.get(url, params=params, timeout=timeout)  # the network call
        except (httpx.TimeoutException, httpx.TransportError) as exc:  # transient network failure
            if attempt > retry_max:  # exhausted retries
                raise EodhdTransientError(f"{ctx}: network failure after {retry_max} retries: {exc!r}") from exc
            await asyncio.sleep(_backoff_seconds(attempt))  # back off, then retry
            continue
        status = resp.status_code  # the HTTP status to classify
        if status == 200:  # success
            try:
                return resp.json()  # parsed JSON (list for data endpoints)
            except ValueError as exc:  # body was not valid JSON
                raise EodhdShapeError(
                    f"{ctx}: non-JSON body params={_redact(params)} body={resp.text[:200]!r}"
                ) from exc
        if status in (401, 403):  # auth failure -> fail the whole run
            # Include the body for diagnostics: a daily-limit response could plausibly arrive as a 403,
            # which this branch absorbs (run-fatal). We do NOT reclassify it (we have not verified that
            # 403 means daily-limit), but we record the body so the first occurrence is diagnosable.
            raise EodhdAuthError(
                f"{ctx}: HTTP {status} (auth) params={_redact(params)} body={resp.text[:200]!r}"
            )
        if status == 422:  # span-cap guard -> programming error, never retried
            raise EodhdCapError(
                f"{ctx}: HTTP 422 (span cap) params={_redact(params)} body={resp.text[:200]!r}"
            )
        if status == 429 or 500 <= status < 600:  # rate-limited or server error -> transient
            if attempt > retry_max:  # exhausted retries
                # Capture the body + any Retry-After header: a real daily-limit-exceeded response may
                # arrive here as a 429. We retry-then-fail it (the bounded transient path) rather than
                # guessing it is the cap, but record the ground truth so it can be classified later.
                retry_after = resp.headers.get("Retry-After", "")  # advisory wait, if the server sent one
                raise EodhdTransientError(
                    f"{ctx}: HTTP {status} after {retry_max} retries params={_redact(params)} "
                    f"retry_after={retry_after!r} body={resp.text[:200]!r}"
                )
            await asyncio.sleep(_backoff_seconds(attempt))  # back off, then retry
            continue
        raise EodhdShapeError(  # any other 4xx -> unexpected, not retried
            f"{ctx}: HTTP {status} params={_redact(params)} body={resp.text[:200]!r}"
        )


def _parse_usage(data) -> tuple:
    """Parse a /user payload into (used, limit, extra, requests_date) or raise EodhdShapeError.

    Pure (no I/O) so the probe asserts the shape guard fully offline (the build_report_line precedent).
    apiRequestsDate is REQUIRED, not defaulted: it is the UTC date the apiRequests count is FOR, and the
    seed logic relies on it to detect a stale (prior-day) count. A payload lacking it is a shape error,
    never a silent assumption (no-fallback rule).
    """
    try:
        used = int(data["apiRequests"])  # calls consumed on the counter's date (global across the key)
        limit = int(data["dailyRateLimit"])  # the daily cap
        extra = int(data.get("extraLimit", 0))  # purchased extra calls, if any
        requests_date = str(data["apiRequestsDate"])  # the UTC date the count is FOR (the staleness signal)
    except (KeyError, TypeError, ValueError) as exc:  # specific shape failure
        raise EodhdShapeError(f"user-api: unexpected usage shape: {data!r}") from exc
    return used, limit, extra, requests_date  # the four numbers the cap accounting + staleness rest on


async def read_usage(http: httpx.AsyncClient, key: str, timeout: float) -> tuple:
    """Read the live usage counter. Returns (apiRequests, dailyRateLimit, extraLimit, apiRequestsDate)."""
    data = await fetch_json(http, USER_URL, {"api_token": key, "fmt": "json"}, "user-api", timeout)  # live read
    return _parse_usage(data)  # parse + shape-guard (pure, separately testable)
