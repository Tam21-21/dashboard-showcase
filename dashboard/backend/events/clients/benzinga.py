"""Benzinga ratings-calendar rate-limited HTTP client (the Benzinga ingestion unit).

Mirrors EdgarClient (events/clients/edgar.py): a single-process, wall-clock-spaced GET over
stdlib urllib, with 429 backoff, 5xx fail-fast, and a socket timeout. The token is passed in
by the fetcher (which reads BENZINGA_TOKEN from the env); the client never reads the
environment and never logs the token (NFR-1 — every log/error line routes the URL through
`_safe_url`, which redacts the token).

Endpoint: GET https://api.benzinga.com/api/v2.1/calendar/ratings, auth via the `token` query
parameter. The response is `{"ratings": [...]}` when populated and a bare `[]` when a window
is empty (both shapes verified live, 2026-06-05). These transport errors are caught by the
fetcher BEFORE any batch opens, so they never become a research_event_batches.failure_class
(no failure_class CHECK-enum entry is needed for them).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger("dashboard.benzinga.client")

# The free-tier ratings calendar endpoint (verified live).
BENZINGA_RATINGS_URL = "https://api.benzinga.com/api/v2.1/calendar/ratings"


class BenzingaHttp429Persistent(RuntimeError):
    """Benzinga returned 429 beyond the retry budget; the fetcher fails the run."""


class BenzingaHttp5xx(RuntimeError):
    """Benzinga returned a 5xx; the fetcher fails the run (no per-record quarantine)."""


class BenzingaNetworkTimeout(RuntimeError):
    """A socket timeout or network failure reaching Benzinga."""


class BenzingaResponseInvalid(RuntimeError):
    """The Benzinga response was not the expected JSON object/array shape (or a 4xx)."""


class BenzingaClient:
    """Rate-limited GET client for the Benzinga ratings calendar.

    Single-process wall-clock spacer (the fetcher is a oneshot, like the EDGAR unit), so a
    simple inter-request floor is sufficient. The token is held privately and redacted from
    every emitted string.
    """

    def __init__(self, *,
                 token: str,
                 base_url: str = BENZINGA_RATINGS_URL,
                 min_interval_s: float = 0.5,
                 backoff_429_seconds: int = 60,
                 max_retries: int = 2,
                 timeout_s: float = 30.0):
        if not token:                                          # a missing token is a config error; fail at construction
            raise ValueError("BenzingaClient requires a non-empty token")
        self._token = token                                    # private; never logged, never in an error string
        self.base_url = base_url
        self.min_interval_s = min_interval_s
        self.backoff_429_seconds = backoff_429_seconds
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self._last_request_at: Optional[float] = None          # wall-clock of the previous request (spacer state)

    def _spacer_sleep(self) -> None:
        """Block until min_interval_s has elapsed since the previous request."""
        if self._last_request_at is None:                      # first request: no spacer
            return
        gap = self.min_interval_s - (time.time() - self._last_request_at)
        if gap > 0:
            time.sleep(gap)

    def _safe_url(self, url: str) -> str:
        """The URL with the token redacted, for logs and error messages (NFR-1)."""
        return url.replace(self._token, "<token>") if self._token else url

    def get_ratings(self, *,
                    date_from: Optional[str] = None,
                    date_to: Optional[str] = None,
                    tickers: Optional[str] = None,
                    page: int = 0,
                    pagesize: int = 1000) -> list[dict]:
        """One rate-limited GET against calendar/ratings. Returns the ratings list ([] on empty)."""
        params = {"token": self._token, "page": str(page), "pagesize": str(pagesize)}
        if date_from is not None:                              # Benzinga's bracketed parameter syntax
            params["parameters[date_from]"] = date_from
        if date_to is not None:
            params["parameters[date_to]"] = date_to
        if tickers is not None:
            params["parameters[tickers]"] = tickers
        url = self.base_url + "?" + urllib.parse.urlencode(params)

        attempts = 0
        raw = b""
        while True:
            self._spacer_sleep()
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            attempts += 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                    raw = resp.read()
                self._last_request_at = time.time()
                break
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.time()
                if exc.code == 429:
                    if attempts > self.max_retries:
                        raise BenzingaHttp429Persistent(
                            f"Benzinga 429 persistent after {attempts} attempts on "
                            f"{self._safe_url(url)}"
                        ) from exc
                    log.warning("benzinga 429: backing off %ds", self.backoff_429_seconds)
                    time.sleep(self.backoff_429_seconds)
                    continue
                if 500 <= exc.code <= 599:
                    raise BenzingaHttp5xx(
                        f"Benzinga {exc.code} {exc.reason} on {self._safe_url(url)}"
                    ) from exc
                # 4xx other than 429 (e.g. 401 bad token, 404): a request-level failure.
                raise BenzingaResponseInvalid(
                    f"Benzinga {exc.code} {exc.reason} on {self._safe_url(url)}"
                ) from exc
            except urllib.error.URLError as exc:
                self._last_request_at = time.time()
                raise BenzingaNetworkTimeout(
                    f"Benzinga network failure on {self._safe_url(url)}: "
                    f"{getattr(exc, 'reason', exc)!r}"
                ) from exc
            except TimeoutError as exc:
                self._last_request_at = time.time()
                raise BenzingaNetworkTimeout(
                    f"Benzinga socket timeout on {self._safe_url(url)} after {self.timeout_s}s"
                ) from exc

        # Parse + normalize the two known response shapes.
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BenzingaResponseInvalid(
                f"Benzinga response was not valid JSON on {self._safe_url(url)}"
            ) from exc
        if isinstance(data, dict):                             # populated window: {"ratings": [...]}
            ratings = data.get("ratings", [])
        elif isinstance(data, list):                           # empty window: a bare []
            ratings = data
        else:
            raise BenzingaResponseInvalid(
                f"Benzinga response was neither object nor array on {self._safe_url(url)}"
            )
        if not isinstance(ratings, list):
            raise BenzingaResponseInvalid(
                f"Benzinga 'ratings' was not a list on {self._safe_url(url)}"
            )
        return ratings
