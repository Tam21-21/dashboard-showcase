"""Finary refresh (lane FR1a): start the claude.ai task "Finary money sync" through its API trigger.

The task reads Finary and posts the new read to the ingest route, as it does twice a day. This module
only fires the trigger. It writes no row and never logs or returns the key.
Reference: Anthropic, "Trigger a routine through the API" (POST .../routines/{id}/fire).
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

FIRE_PREFIX = "https://api.anthropic.com/v1/claude_code/routines/"
API_VERSION = "2023-06-01"
# The call answers once the run is created; it does not wait for the run to finish.
TIMEOUT_SECONDS = 20

NOT_SET = "The Finary refresh is not set up on the server yet."
BAD_ADDRESS = "The Finary refresh address on the server is not a Claude task address."


class RefreshError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS)


def _json(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _message(body: dict) -> str:
    error = body.get("error")
    text = error.get("message") if isinstance(error, dict) else None
    return text if isinstance(text, str) and text else "no reason given"


def _wait_words(header: Optional[str]) -> str:
    if header is not None and header.strip().isdigit():
        minutes = max(1, math.ceil(int(header.strip()) / 60))
        unit = "minute" if minutes == 1 else "minutes"
        return f"Try again in {minutes} {unit}."
    return "Try again later."


def fire(now: Optional[datetime] = None) -> dict:
    url = os.environ.get("MONEY_SYNC_FIRE_URL", "").strip()
    token = os.environ.get("MONEY_SYNC_FIRE_TOKEN", "").strip()
    if not url or not token:
        raise RefreshError(503, NOT_SET)
    if not (url.startswith(FIRE_PREFIX) and url.endswith("/fire")):
        raise RefreshError(503, BAD_ADDRESS)
    try:
        with _client() as client:
            resp = client.post(
                url, json={},
                headers={"Authorization": f"Bearer {token}", "anthropic-version": API_VERSION},
            )
    except httpx.TimeoutException as exc:
        raise RefreshError(504, f"Claude did not answer in {TIMEOUT_SECONDS} seconds.") from exc
    except httpx.HTTPError as exc:
        raise RefreshError(502, "Claude could not be reached.") from exc

    body = _json(resp)
    status = resp.status_code
    if status == 200:
        link = body.get("claude_code_session_url")
        if not isinstance(link, str) or not link:
            raise RefreshError(502, "Claude started the refresh but did not say where it runs.")
        asked = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"asked_at": asked, "run_url": link}
    if status == 401:
        raise RefreshError(
            502, "Claude refused the refresh key. Make a new key for the Finary task and put it on the server."
        )
    if status == 429:
        raise RefreshError(429, f"Too many refreshes this hour. {_wait_words(resp.headers.get('Retry-After'))}")
    if status == 400:
        raise RefreshError(502, f"Claude did not start the Finary task: {_message(body)}")
    if status >= 400:
        raise RefreshError(502, f"Claude answered {status}: {_message(body)}")
    raise RefreshError(502, f"Claude answered {status}: no reason given")
