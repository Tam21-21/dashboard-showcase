"""Settings page: which connectors have their keys on the server, and what each one is doing.

GET /connections answers only whether a key is set; a key on the server is not a tested connection.
GET /connections/live asks each service what is true now (the Garmin sync state from our own
database, the Alpaca paper account, the EODHD request count). No key value is ever read into a
response, and Alpaca is only ever asked for its account on the paper host: no order, no live host.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter

from .. import garmin

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

_ALPACA_ACCOUNT_URL = "https://paper-api.alpaca.markets/v2/account"
_EODHD_USER_URL = "https://eodhd.com/api/user"
_TIMEOUT_S = 5
_CACHE_S = 60

# name -> (monotonic time of the answer, the answer, error sentence included). A failed call is
# cached too, so a broken key never costs one call per page view.
_CACHE: dict[str, tuple[float, dict]] = {}


def _set(name: str) -> bool:
    return os.environ.get(name, "") != ""


@router.get("/connections")
def get_connections():
    return {
        "alpaca": _set("ALPACA_API_KEY") and _set("ALPACA_SECRET_KEY"),
        "eodhd": _set("EODHD_API_KEY"),
        "garmin": garmin.is_configured(),
    }


def _cached(name: str, fetch) -> dict:
    now = time.monotonic()
    hit = _CACHE.get(name)
    if hit is not None and now - hit[0] < _CACHE_S:
        return hit[1]
    answer = fetch()
    _CACHE[name] = (time.monotonic(), answer)
    return answer


def _get(service: str, url: str, **kwargs):
    """One GET. Returns (response, None) or (None, one plain sentence). The sentence never holds the key."""
    try:
        return httpx.get(url, timeout=_TIMEOUT_S, **kwargs), None
    except httpx.TimeoutException:
        return None, f"{service} did not answer in {_TIMEOUT_S} s"
    except httpx.RequestError:
        return None, f"{service} could not be reached"


def _alpaca_fetch(key: str, secret: str) -> dict:
    answer = {"configured": True, "paper": True, "status": None, "trading_blocked": None, "error": None}
    if not key.startswith("PK"):
        answer["error"] = "The Alpaca key is not a paper key"
        return answer
    resp, error = _get(
        "Alpaca paper",
        _ALPACA_ACCOUNT_URL,
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
    )
    if error is not None:
        answer["error"] = error
    elif resp.status_code != 200:
        answer["error"] = f"Alpaca paper answered HTTP {resp.status_code}"
    else:
        body = resp.json()
        answer["status"] = body["status"]
        answer["trading_blocked"] = body["trading_blocked"]
    return answer


def _alpaca_state() -> dict:
    if not (_set("ALPACA_API_KEY") and _set("ALPACA_SECRET_KEY")):
        return {"configured": False, "paper": None, "status": None, "trading_blocked": None, "error": None}
    key, secret = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    return _cached("alpaca", lambda: _alpaca_fetch(key, secret))


def _eodhd_fetch(token: str) -> dict:
    answer = {"configured": True, "requests_today": None, "daily_limit": None, "as_of": None, "error": None}
    resp, error = _get("EODHD", _EODHD_USER_URL, params={"api_token": token, "fmt": "json"})
    if error is not None:
        answer["error"] = error
    elif resp.status_code != 200:
        answer["error"] = f"EODHD answered HTTP {resp.status_code}"
    else:
        body = resp.json()
        answer["requests_today"] = body["apiRequests"]
        answer["daily_limit"] = body["dailyRateLimit"]
        answer["as_of"] = body["apiRequestsDate"]
    return answer


def _eodhd_state() -> dict:
    if not _set("EODHD_API_KEY"):
        return {"configured": False, "requests_today": None, "daily_limit": None, "as_of": None, "error": None}
    token = os.environ["EODHD_API_KEY"]
    return _cached("eodhd", lambda: _eodhd_fetch(token))


def _isolated(service: str, part, empty: dict) -> dict:
    """Run one part so its failure never fails the others (DECISIONS #432: isolate one item, log the
    full error, show it where a person can see it). A part's own known failures are already sentences."""
    try:
        return part()
    except Exception:
        log.exception("connections live: the %s part failed", service)
        return {**empty, "error": f"{service} could not be read, see the server log"}


@router.get("/connections/live")
def get_connections_live():
    return {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "garmin": _isolated(
            "Garmin",
            garmin.sync_state,
            {"configured": garmin.is_configured(), "last_sync_at": None, "last_sync_error": None, "watch_data_until": None},
        ),
        "alpaca": _isolated(
            "Alpaca paper",
            _alpaca_state,
            {"configured": True, "paper": True, "status": None, "trading_blocked": None},
        ),
        "eodhd": _isolated(
            "EODHD",
            _eodhd_state,
            {"configured": True, "requests_today": None, "daily_limit": None, "as_of": None},
        ),
    }
