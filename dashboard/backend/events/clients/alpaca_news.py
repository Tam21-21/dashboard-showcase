"""Alpaca news API rate-limited HTTP client (F7). Mirrors BenzingaClient / EdgarClient.

A single-process, wall-clock-spaced GET over stdlib urllib, with 429 backoff, 5xx fail-fast,
and a socket timeout. Differs from BenzingaClient in two ways the API forces: auth is via
HEADERS (APCA-API-KEY-ID / APCA-API-SECRET-KEY), not a query token; and pagination is by an
opaque next_page_token, not page numbers. The keys are passed in by the fetcher (which reads
ALPACA_API_KEY / ALPACA_SECRET_KEY from the env — the same keys the price fetcher uses); the
client never reads the environment and never logs a key (NFR-1: keys are redacted from every
emitted string).

Endpoint: GET https://data.alpaca.markets/v1beta1/news. Response is
`{"news": [...], "next_page_token": "<base64>"|null}` (verified live 2026-06-17). Free plan:
200 req/min. Transport errors are caught by the fetcher BEFORE any batch opens.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger("dashboard.alpaca_news.client")

# The Alpaca news endpoint (Benzinga-powered; verified live).
ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"


class AlpacaNewsHttp429Persistent(RuntimeError):
    """Alpaca returned 429 beyond the retry budget; the fetcher fails the run."""


class AlpacaNewsHttp5xx(RuntimeError):
    """Alpaca returned a 5xx; the fetcher fails the run (no per-record quarantine)."""


class AlpacaNewsNetworkTimeout(RuntimeError):
    """A socket timeout or network failure reaching Alpaca."""


class AlpacaNewsResponseInvalid(RuntimeError):
    """The Alpaca response was not the expected JSON object shape (or a non-429 4xx)."""


class AlpacaNewsClient:
    """Rate-limited GET client for the Alpaca news API.

    Single-process wall-clock spacer (the fetcher is a oneshot, like the EDGAR/Benzinga units),
    so a simple inter-request floor is sufficient. The keys are held privately and never logged.
    """

    def __init__(self, *,
                 key_id: str,
                 secret_key: str,
                 base_url: str = ALPACA_NEWS_URL,
                 min_interval_s: float = 0.4,
                 backoff_429_seconds: int = 60,
                 max_retries: int = 2,
                 timeout_s: float = 30.0):
        if not key_id or not secret_key:                       # missing keys are a config error; fail at construction
            raise ValueError("AlpacaNewsClient requires a non-empty key_id and secret_key")
        self._key_id = key_id                                  # private; never logged
        self._secret_key = secret_key                          # private; never logged
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

    def _redact(self, text: str) -> str:
        """Redact both keys from any string headed for a log or error message (NFR-1)."""
        out = text
        if self._key_id:
            out = out.replace(self._key_id, "<key-id>")
        if self._secret_key:
            out = out.replace(self._secret_key, "<secret>")
        return out

    def get_news(self, *,
                 symbols: str,
                 start: Optional[str] = None,
                 end: Optional[str] = None,
                 page_token: Optional[str] = None,
                 limit: int = 50,
                 sort: str = "asc",
                 include_content: bool = False) -> tuple[list[dict], Optional[str]]:
        """One rate-limited GET. Returns (articles, next_page_token).

        `symbols` is the comma-joined symbol set. `start`/`end` are RFC-3339 or YYYY-MM-DD.
        `sort='asc'` gives deterministic backfill paging. `limit` is capped at 50 (the API max).
        `include_content=False` — we never store the article body. `next_page_token` is the opaque
        continuation cursor (None when the window is exhausted).
        """
        params = {
            "symbols": symbols,
            "limit": str(min(limit, 50)),
            "sort": sort,
            "include_content": "true" if include_content else "false",
        }
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        if page_token is not None:
            params["page_token"] = page_token
        url = self.base_url + "?" + urllib.parse.urlencode(params)
        headers = {
            "APCA-API-KEY-ID": self._key_id,
            "APCA-API-SECRET-KEY": self._secret_key,
            "Accept": "application/json",
        }

        attempts = 0
        raw = b""
        while True:
            self._spacer_sleep()
            request = urllib.request.Request(url, headers=headers)
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
                        raise AlpacaNewsHttp429Persistent(
                            f"Alpaca news 429 persistent after {attempts} attempts on "
                            f"{self._redact(url)}"
                        ) from exc
                    log.warning("alpaca news 429: backing off %ds", self.backoff_429_seconds)
                    time.sleep(self.backoff_429_seconds)
                    continue
                if 500 <= exc.code <= 599:
                    raise AlpacaNewsHttp5xx(
                        f"Alpaca news {exc.code} {exc.reason} on {self._redact(url)}"
                    ) from exc
                # 4xx other than 429 (e.g. 401 bad key, 403, 422): a request-level failure.
                raise AlpacaNewsResponseInvalid(
                    f"Alpaca news {exc.code} {exc.reason} on {self._redact(url)}"
                ) from exc
            except urllib.error.URLError as exc:
                self._last_request_at = time.time()
                raise AlpacaNewsNetworkTimeout(
                    f"Alpaca news network failure on {self._redact(url)}: "
                    f"{getattr(exc, 'reason', exc)!r}"
                ) from exc
            except TimeoutError as exc:
                self._last_request_at = time.time()
                raise AlpacaNewsNetworkTimeout(
                    f"Alpaca news socket timeout on {self._redact(url)} after {self.timeout_s}s"
                ) from exc

        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise AlpacaNewsResponseInvalid(
                f"Alpaca news response was not valid JSON on {self._redact(url)}"
            ) from exc
        if not isinstance(data, dict):
            raise AlpacaNewsResponseInvalid(
                f"Alpaca news response was not a JSON object on {self._redact(url)}"
            )
        articles = data.get("news", [])
        if not isinstance(articles, list):
            raise AlpacaNewsResponseInvalid(
                f"Alpaca news 'news' was not a list on {self._redact(url)}"
            )
        next_token = data.get("next_page_token")               # opaque cursor; None when exhausted
        if next_token is not None and not isinstance(next_token, str):
            raise AlpacaNewsResponseInvalid(
                f"Alpaca news 'next_page_token' was not a string on {self._redact(url)}"
            )
        return articles, next_token
