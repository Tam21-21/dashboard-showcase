"""SEC EDGAR rate-limited HTTP client + R14 timezone helper.

Brief D per spec brief v4 §5 + §10.0 + arch v6 §5.2 rate-limit profile.

Public surface
--------------
EdgarClient(user_agent, max_rps=8, min_interval_s=0.200,
            backoff_429_seconds=60, max_retries=1, timeout_s=30,
            max_transient_retries=3, transient_backoff_base_s=1.0,
            transient_backoff_cap_s=30.0)
  .get(url) -> bytes
  .get_form_idx(year, quarter) -> str
  .get_submission_text(filer_cik, accession_number) -> str

parse_acceptance_datetime(s: str) -> int
  Convert an EDGAR SGML-header `ACCEPTANCE-DATETIME` string (`YYYYMMDDHHMMSS`
  Eastern wall-clock; no offset suffix) to UTC epoch seconds via stdlib
  zoneinfo, per spec brief §10.0 / R14.

Why these knobs
---------------
- max_rps=8 / min_interval_s=0.200: the verified SEC ceiling is 10 req/sec
  (`sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data`,
  resolved in §10 research). 8 is the safety margin. 200 ms inter-request
  floor enforces 5 rps in steady state (the rate limiter clamps wall-clock
  spacing between requests, not a token bucket — the floor is the binding
  constraint).
- backoff_429_seconds=60 / max_retries=1: arch §5.2 fail-fast on a sustained
  rate-limit hit; a second 429 raises EdgarHttp429Persistent and the batch
  closes failed. The next timer firing retries from scratch.
- timeout_s=30: covers slow EDGAR mirrors without hanging the fetcher
  indefinitely; matches the cli.py load-cik-ticker-map timeout for symmetry.

Why stdlib `urllib`, not `httpx`
--------------------------------
The cli.py `load-cik-ticker-map` subcommand (brief C) already uses urllib
for SEC's company_tickers_exchange.json. Same surface here keeps the
EDGAR-touching code single-stack: the gzip / deflate decode is done explicitly
via `decompress_sec_body` (urllib does NOT auto-decode Content-Encoding); the
User-Agent header is one extra arg; the retry path is one
explicit `except urllib.error.HTTPError` clause. Adding httpx for ~80 LOC
worth of urllib is dependency surface for no functional gain.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import re
import random                                                    # full-jitter for the transient-retry backoff (item 5b)
import time
import urllib.error
import urllib.request
import zlib
from typing import Optional
from zoneinfo import ZoneInfo

from ..errors import (
    EdgarHttp5xx,
    EdgarHttp429Persistent,
    EdgarNetworkTimeout,
    EdgarParseFatal,
)

log = logging.getLogger("dashboard.edgar.client")


# EDGAR's permanent archive base. The form.idx and submission .txt URLs both
# live under here; centralising the host keeps a future mirror-switch (e.g. to
# a Fastly cache) to one edit.
EDGAR_ARCHIVE_BASE = "https://www.sec.gov/Archives"
EDGAR_FULL_INDEX_BASE = f"{EDGAR_ARCHIVE_BASE}/edgar/full-index"

# EDGAR's structured-data subdomain. The XBRL CompanyFacts API (M3.75 fundamentals)
# lives here, NOT on the /Archives host the 13D form.idx and submission .txt paths use.
# Centralised so a future mirror-switch is one edit, mirroring the archive bases above.
EDGAR_DATA_BASE = "https://data.sec.gov"

# R14 / §10.0 fixed zone. EDGAR timestamps are always Eastern (EST or EDT
# depending on the date); `America/New_York` is the IANA name zoneinfo
# resolves both seasonal offsets from a single zone.
# The file name of an older submissions page (filings.files[].name): CIK, ten digits, -submissions-, three digits.
_SUBMISSIONS_PAGE_NAME = re.compile(r"CIK\d{10}-submissions-\d{3}\.json")

EASTERN_ZONE = ZoneInfo("America/New_York")
UTC_ZONE = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Helpers — R14 timezone conversion (verified against spec brief §10.0 fixture)
# ---------------------------------------------------------------------------


def parse_acceptance_datetime(s: str) -> int:
    """Convert an SGML `YYYYMMDDHHMMSS` Eastern wall-clock string to UTC epoch.

    Discipline per spec brief §10.0:
      1. Parse the digit string into a naive datetime (no offset on the wire).
      2. Localize to America/New_York via zoneinfo (DST-aware; no pytz dep).
      3. Convert to UTC; return epoch seconds.

    Verified against the §10.0 fixture: '20240312163142' → 1710275502 (March 12
    2024 is EDT = UTC-4; 16:31:42 EDT = 20:31:42 UTC → epoch 1710275502).

    The brief D probe also runs a non-DST fixture (a January date) to exercise
    the EST branch and confirm zoneinfo picks the right offset by date.

    Raises ValueError if the input does not parse as YYYYMMDDHHMMSS (caller
    maps to EdgarParseFatal at the per-record path).
    """
    stripped = s.strip()
    if len(stripped) != 14 or not stripped.isdigit():
        raise ValueError(
            f"parse_acceptance_datetime: expected YYYYMMDDHHMMSS digit string, "
            f"got {stripped!r}"
        )
    naive = dt.datetime.strptime(stripped, "%Y%m%d%H%M%S")
    # `tzinfo=EASTERN_ZONE` localizes the naive value: zoneinfo picks the
    # right offset (EST vs EDT) from the date itself.
    eastern = naive.replace(tzinfo=EASTERN_ZONE)
    utc = eastern.astimezone(UTC_ZONE)
    return int(utc.timestamp())


# ---------------------------------------------------------------------------
# Helper — SEC response decompression (the single owner of the gotcha)
# ---------------------------------------------------------------------------


def decompress_sec_body(raw_body: bytes, content_encoding: str | None) -> bytes:
    """Decode a possibly gzip/deflate-compressed SEC response body.

    Stdlib `urllib` does NOT auto-decode the `Content-Encoding` of a response —
    that transparent decode is a `requests` / `httpx` feature, not a stdlib one.
    So every SEC fetch path that sends `Accept-Encoding: gzip, deflate` (to cut
    SEC bandwidth) must decode the body itself, or it hands compressed bytes to
    a JSON / text parser and fails on the gzip magic byte (0x8b). This is the one
    owner of that decode so both `EdgarClient.get` and the cik-ticker-map loader
    (`events/cik_ticker_map.py`) share identical, tested behavior.

    `content_encoding` is the raw `Content-Encoding` header value (or None when
    the server returned the body uncompressed); matching is case-insensitive.
    """
    encoding = (content_encoding or "").lower()                 # normalize; None -> "" -> passthrough
    if encoding == "gzip":
        return gzip.decompress(raw_body)                        # the SEC mirror's usual encoding
    if encoding == "deflate":
        # Some servers send raw deflate (no zlib header); try zlib first, then
        # fall back to raw inflate.
        try:
            return zlib.decompress(raw_body)
        except zlib.error:
            return zlib.decompress(raw_body, -zlib.MAX_WBITS)
    return raw_body                                             # served uncompressed (or unknown encoding)


# ---------------------------------------------------------------------------
# Rate-limited HTTP client
# ---------------------------------------------------------------------------


class EdgarClient:
    """Rate-limited HTTP client targeting `sec.gov`.

    Single-threaded; the rate-limit gate is a simple wall-clock spacer. The
    EDGAR fetcher script is single-process (per arch §5.2's systemd-timer-
    oneshot pattern) so contention against the spacer is non-issue.

    Each `get()` call:
      1. Blocks until at least `min_interval_s` has elapsed since the previous
         call (the 200 ms floor in spec brief §5).
      2. Issues a GET with `User-Agent`, `Accept-Encoding: gzip, deflate` (per
         §10's verified headers).
      3. On 429: sleeps `backoff_429_seconds`, retries up to `max_retries`. A
         second 429 raises EdgarHttp429Persistent.
      4. On 5xx or socket timeout (transient, item 5b): retries up to
         `max_transient_retries` with exponential full-jitter backoff (an
         idempotent GET is safe to retry, kleppmann_canon.md:1133), then raises
         EdgarHttp5xx / EdgarNetworkTimeout once the budget is exhausted. The
         per-filing ceiling the fetcher's K-consecutive-failure abort counts against.
      5. On other 4xx (e.g. 404): raises EdgarParseFatal (the per-record path
         quarantines the row); no retry.

    Returns the response bytes (gzip already decoded by http.client). Caller
    decodes to text if appropriate (form.idx is latin-1; submission .txt is
    latin-1 by convention even though the cover-page body may carry UTF-8).
    """

    def __init__(self, *,
                 user_agent: str,
                 max_rps: float = 8.0,
                 min_interval_s: float = 0.200,
                 backoff_429_seconds: int = 60,
                 max_retries: int = 1,
                 timeout_s: float = 30.0,
                 # Item 5b (D10) — bounded retry for a TRANSIENT 5xx / socket timeout on an
                 # idempotent GET (kleppmann_canon.md:1133 — a read GET is safe to retry).
                 # Exponential backoff with FULL jitter avoids a retry storm against SEC
                 # fair-access (kleppmann_canon.md:220,270). These are OPERATIONAL reliability
                 # knobs, not backtest parameters, so they are documented here, not in
                 # parameter_provenance.md (Cowork M4). Defaults: up to 3 retries (4 total
                 # attempts) with the ceiling doubling 1s -> 2s -> 4s, capped at 30s. The client
                 # already paces at min_interval_s=0.200 (~5 rps) under SEC's ~10 rps ceiling, so
                 # a few seconds of jittered backoff rides out a momentary EDGAR blip without
                 # stalling a 100k-filing backfill. Worst-case added wait per genuinely-failing GET
                 # is ~8s: the three jittered backoff ceilings (1+2+4s) plus the per-attempt
                 # min_interval_s spacer (~0.2s before each retry) — the spacer is intentional, it
                 # keeps every retry under SEC fair-access pacing (CQ-SF1).
                 max_transient_retries: int = 3,
                 transient_backoff_base_s: float = 1.0,
                 transient_backoff_cap_s: float = 30.0):
        if not user_agent or "@" not in user_agent:
            # EL38 / §10 verified: SEC fair-access policy requires a declared
            # User-Agent with an email contact. An empty or contactless UA is a
            # config error; fail at construction so a fetcher run does not get
            # past the gate.
            raise ValueError(
                "EdgarClient requires a User-Agent containing an email "
                "contact (SEC fair-access policy); got: "
                f"{user_agent!r}"
            )
        if transient_backoff_base_s <= 0:
            # A non-positive base would make every backoff ceiling 0 -> zero-delay retries (the
            # docstring guarantees a bounded exponential backoff). Fail at construction (CQ-M1),
            # mirroring the user_agent gate above, rather than silently retrying with no delay.
            raise ValueError(
                "EdgarClient transient_backoff_base_s must be > 0; "
                f"got {transient_backoff_base_s!r}"
            )
        self.user_agent = user_agent
        self.max_rps = max_rps
        self.min_interval_s = min_interval_s
        self.backoff_429_seconds = backoff_429_seconds
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.max_transient_retries = max_transient_retries       # 5xx / timeout retry budget (item 5b)
        self.transient_backoff_base_s = transient_backoff_base_s  # exponential base for the backoff ceiling
        self.transient_backoff_cap_s = transient_backoff_cap_s   # the ceiling never grows past this
        # Wall-clock of the previous request's completion. The first request
        # has no spacer; subsequent ones sleep up to min_interval_s.
        self._last_request_at: Optional[float] = None

    def _spacer_sleep(self) -> None:
        """Block until min_interval_s has elapsed since the previous request."""
        if self._last_request_at is None:
            return
        elapsed = time.time() - self._last_request_at
        gap = self.min_interval_s - elapsed
        if gap > 0:
            time.sleep(gap)

    def _sleep_transient_backoff(self, attempt: int, reason: str, url: str) -> None:
        """Sleep an exponential, full-jittered delay before retrying a transient failure (item 5b).

        Ceiling doubles per attempt (base * 2^(attempt-1)) capped at transient_backoff_cap_s; the
        actual sleep is a uniform draw in [0, ceiling] (full jitter), so retries against SEC spread
        out instead of synchronizing into a storm (kleppmann_canon.md:220,270). `attempt` is 1-based.
        """
        ceiling = min(self.transient_backoff_cap_s,
                      self.transient_backoff_base_s * (2 ** (attempt - 1)))
        delay = random.uniform(0, ceiling)                       # full jitter in [0, ceiling]
        log.warning("edgar transient (%s) on %s: retry %d/%d after %.2fs",
                    reason, url, attempt, self.max_transient_retries, delay)
        time.sleep(delay)

    def get(self, url: str) -> bytes:
        """Rate-limited GET. Returns decoded body bytes.

        Per-call lifecycle: spacer sleep, build request, send, decode, retry
        on 429. Updates `_last_request_at` on completion (whether success or
        a clean failure path) so the next call honors the spacer regardless.
        """
        # Two retry budgets: 429 (rate-limit, long fixed backoff) and TRANSIENT 5xx/timeout
        # (server/network blip, short exponential+jitter backoff — item 5b). Kept separate so a
        # 429 does not consume the transient budget or vice versa. The transient budget is shared
        # across 5xx AND socket-timeout for THIS one GET on purpose (PC-SF1): it bounds total
        # retries per request, so a flapping endpoint alternating 5xx/timeout cannot double its
        # retries — the conservative choice against SEC fair-access.
        attempts_429 = 0
        attempts_transient = 0
        while True:
            self._spacer_sleep()
            request = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
                # Accept anything; form.idx is text/plain, submission .txt is
                # text/plain, primary docs are text/html or text/xml.
                "Accept": "*/*",
            })
            try:
                # `timeout=` is the socket-level timeout. urllib does NOT
                # auto-decode Content-Encoding (see decompress_sec_body), so we
                # read the raw body + its encoding header and decode via the
                # shared helper — the per-§10 EL38 `Accept-Encoding: gzip,
                # deflate` request header (which cuts SEC bandwidth ~4x on
                # form.idx) saves bytes without breaking the body.
                with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                    raw_body = resp.read()
                    content_encoding = resp.headers.get("Content-Encoding")
                self._last_request_at = time.time()
                return decompress_sec_body(raw_body, content_encoding)
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.time()
                if exc.code == 429:
                    attempts_429 += 1
                    if attempts_429 > self.max_retries:
                        raise EdgarHttp429Persistent(
                            f"EDGAR 429 persistent after {attempts_429} attempts on {url}"
                        ) from exc
                    log.warning("edgar 429: backing off %ds on %s",
                                self.backoff_429_seconds, url)
                    time.sleep(self.backoff_429_seconds)
                    continue
                if 500 <= exc.code <= 599:
                    # Item 5b: a 5xx is transient. Retry with exponential+jitter backoff up to
                    # max_transient_retries before giving up with EdgarHttp5xx (the per-filing
                    # ceiling the fetcher's K-consecutive abort counts against).
                    attempts_transient += 1
                    if attempts_transient > self.max_transient_retries:
                        raise EdgarHttp5xx(
                            f"EDGAR {exc.code} {exc.reason} on {url} "
                            f"after {attempts_transient} attempts"
                        ) from exc
                    self._sleep_transient_backoff(attempts_transient, f"{exc.code} {exc.reason}", url)
                    continue
                # 4xx other than 429: a 404 typically means the filing is
                # restricted or the URL is wrong — treat as parse-fatal so the
                # per-record path quarantines the row.
                raise EdgarParseFatal(
                    f"EDGAR {exc.code} {exc.reason} on {url}"
                ) from exc
            except urllib.error.URLError as exc:
                self._last_request_at = time.time()
                # URLError wraps both DNS failures and socket-level timeouts.
                # We treat both as network-class transients (item 5b): retry with backoff before
                # giving up with EdgarNetworkTimeout.
                attempts_transient += 1
                reason = getattr(exc, "reason", exc)
                if attempts_transient > self.max_transient_retries:
                    raise EdgarNetworkTimeout(
                        f"EDGAR network failure on {url}: {reason!r} "
                        f"after {attempts_transient} attempts"
                    ) from exc
                self._sleep_transient_backoff(attempts_transient, f"network {reason!r}", url)
                continue
            except TimeoutError as exc:
                # urlopen raises TimeoutError directly on socket timeout for
                # Python 3.10+. Same transient class as URLError (item 5b): retry with backoff.
                self._last_request_at = time.time()
                attempts_transient += 1
                if attempts_transient > self.max_transient_retries:
                    raise EdgarNetworkTimeout(
                        f"EDGAR socket timeout on {url} after {self.timeout_s}s "
                        f"({attempts_transient} attempts)"
                    ) from exc
                self._sleep_transient_backoff(attempts_transient, "socket timeout", url)
                continue

    # ----- URL builders -------------------------------------------------

    @staticmethod
    def form_idx_url(year: int, quarter: int) -> str:
        """Return the canonical form.idx URL for a calendar quarter.

        Per §10 verified path: `/Archives/edgar/full-index/YYYY/QTRn/form.idx`
        from 1994 Q3 (the first quarter SEC publishes a quarterly index for).
        """
        if not 1994 <= year <= 2100:
            raise ValueError(f"year out of expected range: {year}")
        if not 1 <= quarter <= 4:
            raise ValueError(f"quarter must be 1..4, got {quarter}")
        return f"{EDGAR_FULL_INDEX_BASE}/{year}/QTR{quarter}/form.idx"

    @staticmethod
    def submission_txt_url(filer_cik: int, accession_number: str) -> str:
        """Return the full-submission .txt URL for a filing.

        Filename rule (verified at writing): for accession `0001234567-24-000001`
        the .txt sits at `/Archives/edgar/data/{cik_no_pad}/{accession_no_dashes}/
        {accession_dashed}.txt`. `cik_no_pad` is the unpadded integer; the
        accession in the path segment is dashes-stripped; the filename keeps
        the dashes.
        """
        if not accession_number or not isinstance(accession_number, str):
            raise ValueError(f"accession_number must be non-empty str, got {accession_number!r}")
        no_dashes = accession_number.replace("-", "")
        if len(no_dashes) != 18 or not no_dashes.isdigit():
            raise ValueError(
                f"accession_number must be 18 digits after dash strip; got "
                f"{accession_number!r} -> {no_dashes!r}"
            )
        return f"{EDGAR_ARCHIVE_BASE}/edgar/data/{filer_cik}/{no_dashes}/{accession_number}.txt"

    @staticmethod
    def companyfacts_url(cik: int) -> str:
        """Return the XBRL CompanyFacts URL for a CIK (the data-subdomain endpoint).

        Path: `/api/xbrl/companyfacts/CIK{cik:010d}.json` — the CIK zero-padded to 10
        digits (the spike's verified form, `verify_edgar_xbrl_v2.py`). One GET returns the
        company's full tagged history as JSON. Lives on `data.sec.gov` (EDGAR_DATA_BASE),
        not the `/Archives` host the 13D paths use — the M3.75 §3.1 / R1 endpoint.
        """
        if not 0 < cik < 10 ** 10:                                # a CIK is a positive int below 10 digits
            raise ValueError(f"cik out of expected range (1..10^10): {cik!r}")
        return f"{EDGAR_DATA_BASE}/api/xbrl/companyfacts/CIK{cik:010d}.json"

    @staticmethod
    def submissions_json_url(cik: int) -> str:
        """Return the company submissions URL for a CIK: `data.sec.gov/submissions/CIK##########.json`.

        One GET returns the company's recent filings as parallel arrays under `filings.recent`
        (form, items, acceptanceDateTime, accessionNumber, ...). Lives on `data.sec.gov`.
        """
        if not 0 < cik < 10 ** 10:                                # a CIK is a positive int below 10 digits
            raise ValueError(f"cik out of expected range (1..10^10): {cik!r}")
        return f"{EDGAR_DATA_BASE}/submissions/CIK{cik:010d}.json"

    # ----- High-level fetchers ------------------------------------------

    def get_submissions_json(self, cik: int) -> dict:
        """Fetch a company's submissions JSON and return it parsed (a dict).

        Raises EdgarParseFatal when the body is not JSON or not an object. The acceptanceDateTime values
        inside are ISO 8601 UTC ("2026-07-30T20:30:36.000Z"), not the SGML Eastern form that
        parse_acceptance_datetime reads.
        """
        url = self.submissions_json_url(cik)
        body = self.get(url)
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            raise EdgarParseFatal(f"submissions JSON for CIK {cik} is not valid JSON ({url})") from exc
        if not isinstance(parsed, dict):
            raise EdgarParseFatal(f"submissions JSON for CIK {cik} is not an object ({url})")
        return parsed

    def get_submissions_page(self, name: str) -> dict:
        """Fetch one older submissions page by its file name and return it parsed (a dict).

        ``name`` is a ``filings.files[].name`` value from the company's submissions JSON, such as
        ``CIK0000019617-submissions-001.json``. Any other shape raises ValueError before a request goes out
        (the name becomes part of the URL). Raises EdgarParseFatal when the body is not a JSON object. The
        page holds the same parallel arrays as ``filings.recent``, at the top level.
        """
        if not _SUBMISSIONS_PAGE_NAME.fullmatch(name):
            raise ValueError(f"submissions page name must look like CIK0000019617-submissions-001.json; got {name!r}")
        url = f"{EDGAR_DATA_BASE}/submissions/{name}"
        body = self.get(url)
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            raise EdgarParseFatal(f"submissions page {name} is not valid JSON ({url})") from exc
        if not isinstance(parsed, dict):
            raise EdgarParseFatal(f"submissions page {name} is not an object ({url})")
        return parsed

    def get_form_idx(self, year: int, quarter: int) -> str:
        """Fetch a quarter's form.idx, return decoded text (latin-1)."""
        url = self.form_idx_url(year, quarter)
        body = self.get(url)
        # form.idx is plain ASCII with latin-1 fallback for any oddball
        # company-name byte. latin-1 never raises; UTF-8 would on the rare
        # high-bit char.
        return body.decode("latin-1")

    def get_submission_text(self, filer_cik: int, accession_number: str) -> str:
        """Fetch a filing's full-submission .txt, return decoded text (latin-1).

        Submission .txt files are SGML containers with per-document MIME
        boundaries; latin-1 is the safe decode for the header + body
        regardless of the inner document encoding. The caller's parser
        peers at the SGML header (always ASCII) and the cover-page body
        (typically ASCII; high-bit chars are rare and survive latin-1).
        """
        url = self.submission_txt_url(filer_cik, accession_number)
        body = self.get(url)
        return body.decode("latin-1")
