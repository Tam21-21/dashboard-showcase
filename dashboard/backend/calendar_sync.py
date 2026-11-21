"""Google Calendar OAuth2 + sync to local calendar_events table."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError

from .config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
from .userdb import open_rw

log = logging.getLogger("dashboard.calendar")


class GoogleNotConnected(Exception):
    def __init__(self):
        super().__init__("Google Calendar not connected")


class GoogleRefused(Exception):
    def __init__(self, action: str, message: str):
        super().__init__(f"Google refused the {action}: {message}")
        self.action = action
        self.message = message


class EventNotFound(Exception):
    pass

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    # gmail.modify covers gmail.readonly's read access plus the ability to
    # modify labels (needed for marking messages as read on open).
    "https://www.googleapis.com/auth/gmail.modify",
]


def is_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def is_connected() -> bool:
    return bool(_get_pref("google_oauth_tokens"))


def disconnect() -> None:
    """Drop local tokens AND revoke at Google's side so the next Connect forces
    a fresh consent screen (otherwise Google can reuse the existing grant and
    skip the prompt, leaving us stuck on an old set of scopes)."""
    raw = _get_pref("google_oauth_tokens")
    if raw:
        try:
            import json as _json
            import urllib.request, urllib.parse
            tok = _json.loads(raw)
            token = tok.get("refresh_token") or tok.get("token")
            if token:
                data = urllib.parse.urlencode({"token": token}).encode()
                urllib.request.urlopen(
                    "https://oauth2.googleapis.com/revoke",
                    data=data,
                    timeout=5,
                )
        except Exception as e:
            log.warning("Google token revoke failed (continuing): %s", e)

    _del_pref("google_oauth_tokens")
    _del_pref("google_oauth_state")
    _del_pref("google_oauth_code_verifier")


def _client_config() -> dict:
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }


def _pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code_verifier and its S256 code_challenge."""
    verifier = secrets.token_urlsafe(96)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def get_auth_url() -> str:
    from google_auth_oauthlib.flow import Flow

    verifier, challenge = _pkce_pair()

    flow = Flow.from_client_config(
        _client_config(), scopes=SCOPES, redirect_uri=GOOGLE_REDIRECT_URI
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        code_challenge=challenge,
        code_challenge_method="S256",
    )
    _set_pref("google_oauth_state", state)
    _set_pref("google_oauth_code_verifier", verifier)
    return auth_url


def exchange_and_store(code: str, state: str) -> None:
    from google_auth_oauthlib.flow import Flow

    verifier = _get_pref("google_oauth_code_verifier") or ""

    flow = Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
        state=state,
    )
    flow.fetch_token(code=code, code_verifier=verifier)
    _set_pref("google_oauth_tokens", flow.credentials.to_json())
    _del_pref("google_oauth_state")
    _del_pref("google_oauth_code_verifier")


def _get_credentials():
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    raw = _get_pref("google_oauth_tokens")
    if not raw:
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    except Exception:
        return None

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _set_pref("google_oauth_tokens", creds.to_json())
        except RefreshError as e:
            # Token revoked / expired beyond recovery — drop it so the UI flips
            # back to the "Connect" state instead of silently no-op'ing forever.
            log.warning("Google token revoked, clearing stored credentials: %s", e)
            _del_pref("google_oauth_tokens")
            return None
        except Exception as e:
            log.warning("Google credential refresh failed: %s", e)
            return None

    return creds if creds.valid else None


# ── Read ───────────────────────────────────────────────────────────────────────

def get_events(from_date: str, to_date: str) -> list[dict]:
    """Query cached events where date(start_dt) is in [from_date, to_date] (YYYY-MM-DD)."""
    conn = open_rw()
    try:
        rows = conn.execute(
            """SELECT * FROM calendar_events
               WHERE substr(start_dt, 1, 10) >= ?
                 AND substr(start_dt, 1, 10) <= ?
               ORDER BY start_dt""",
            (from_date, to_date),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_today_events() -> list[dict]:
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d")
    return get_events(today, today)


def get_last_synced() -> str | None:
    conn = open_rw()
    try:
        row = conn.execute(
            "SELECT MAX(synced_at) FROM calendar_events"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ── Write ──────────────────────────────────────────────────────────────────────

def sync_events() -> int:
    """Fetch events from Google Calendar and upsert to local DB. Returns count."""
    creds = _get_credentials()
    if creds is None:
        return 0

    from googleapiclient.discovery import build

    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    now = datetime.now(timezone.utc)
    t_min = now - timedelta(days=7)
    t_max = now + timedelta(days=60)

    items: list[dict] = []
    page_token = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=t_min.isoformat(),
                timeMax=t_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                pageToken=page_token,
            )
            .execute()
        )
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    win_s = t_min.strftime("%Y-%m-%d")
    win_e = t_max.strftime("%Y-%m-%d")

    conn = open_rw()
    try:
        fetched_ids: set[str] = set()
        for ev in items:
            fetched_ids.add(ev["id"])
            _upsert_event(conn, ev)

        # Prune deleted/moved events within the sync window — never touch local_only events
        if fetched_ids:
            placeholders = ",".join("?" * len(fetched_ids))
            conn.execute(
                f"""DELETE FROM calendar_events
                    WHERE substr(start_dt,1,10) >= ? AND substr(start_dt,1,10) <= ?
                    AND event_id NOT IN ({placeholders})
                    AND local_only = 0""",
                [win_s, win_e, *fetched_ids],
            )
        else:
            conn.execute(
                """DELETE FROM calendar_events
                   WHERE substr(start_dt,1,10) >= ? AND substr(start_dt,1,10) <= ?
                   AND local_only = 0""",
                [win_s, win_e],
            )
        conn.commit()
    finally:
        conn.close()

    log.info("Calendar sync: %d events", len(items))
    return len(items)


def create_event(
    title: str, start_dt: str, end_dt: str, all_day: bool = False, recurrence: list[str] | None = None
) -> dict:
    """Create an event on Google and cache it. `recurrence` (RRULE lines) goes to Google as given; a
    series then runs one sync so every day shows. Raises GoogleNotConnected or GoogleRefused."""
    creds = _get_credentials()
    if creds is None:
        raise GoogleNotConnected()

    from googleapiclient.discovery import build

    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    if all_day:
        body: dict = {
            "summary": title,
            "start": {"date": start_dt[:10]},
            "end": {"date": end_dt[:10]},
        }
    else:
        body = {
            "summary": title,
            "start": {"dateTime": start_dt, "timeZone": "Europe/Paris"},
            "end": {"dateTime": end_dt, "timeZone": "Europe/Paris"},
        }
    if recurrence:
        body["recurrence"] = recurrence

    try:
        ev = service.events().insert(calendarId="primary", body=body).execute()
    except HttpError as exc:
        raise GoogleRefused("create", exc.reason) from exc
    conn = open_rw()
    try:
        _upsert_event(conn, ev)
        conn.commit()
    finally:
        conn.close()
    # Google has accepted the insert, so a failed refresh is reported next to the event, never as an
    # error: an error would invite a retry that makes a second series.
    sync_error = None
    if recurrence:
        try:
            sync_events()
        except HttpError as exc:
            sync_error = exc.reason
            log.warning("Calendar refresh after creating a series failed: %s", sync_error)
    return {**_row_from_api(ev), "sync_error": sync_error}


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _upsert_event(conn, ev: dict) -> None:
    start = ev.get("start", {})
    end = ev.get("end", {})
    all_day = "date" in start and "dateTime" not in start
    start_dt = start.get("dateTime") or start.get("date", "")
    end_dt = end.get("dateTime") or end.get("date", "")

    conn.execute(
        """INSERT INTO calendar_events
               (event_id, calendar_id, title, start_dt, end_dt, all_day,
                location, description, color_id, html_link, synced_at, local_only)
           VALUES (?, 'primary', ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 0)
           ON CONFLICT (event_id) DO UPDATE SET
               title       = excluded.title,
               start_dt    = excluded.start_dt,
               end_dt      = excluded.end_dt,
               all_day     = excluded.all_day,
               location    = excluded.location,
               description = excluded.description,
               color_id    = excluded.color_id,
               html_link   = excluded.html_link,
               synced_at   = excluded.synced_at,
               local_only  = 0""",
        (
            ev["id"],
            ev.get("summary", "(No title)"),
            start_dt,
            end_dt,
            1 if all_day else 0,
            ev.get("location"),
            ev.get("description"),
            ev.get("colorId"),
            ev.get("htmlLink"),
        ),
    )


def _row_from_api(ev: dict) -> dict:
    start = ev.get("start", {})
    end = ev.get("end", {})
    all_day = "date" in start and "dateTime" not in start
    return {
        "event_id": ev["id"],
        "calendar_id": "primary",
        "title": ev.get("summary", "(No title)"),
        "start_dt": start.get("dateTime") or start.get("date", ""),
        "end_dt": end.get("dateTime") or end.get("date", ""),
        "all_day": 1 if all_day else 0,
        "location": ev.get("location"),
        "description": ev.get("description"),
        "color_id": ev.get("colorId"),
        "html_link": ev.get("htmlLink"),
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }




def push_local_events() -> int:
    """Push local-only events to Google Calendar. Returns count pushed."""
    creds = _get_credentials()
    if creds is None:
        return 0

    conn = open_rw()
    try:
        rows = conn.execute(
            "SELECT * FROM calendar_events WHERE local_only = 1"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return 0

    from googleapiclient.discovery import build
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    pushed = 0
    for row in rows:
        ev = dict(row)
        old_id = ev["event_id"]
        try:
            if ev["all_day"]:
                body: dict = {
                    "summary": ev["title"],
                    "start": {"date": ev["start_dt"][:10]},
                    "end": {"date": ev["end_dt"][:10]},
                }
            else:
                body = {
                    "summary": ev["title"],
                    "start": {"dateTime": ev["start_dt"], "timeZone": "Europe/Paris"},
                    "end": {"dateTime": ev["end_dt"], "timeZone": "Europe/Paris"},
                }
            if ev.get("color_id"):
                body["colorId"] = ev["color_id"]

            result = service.events().insert(calendarId="primary", body=body).execute()

            conn2 = open_rw()
            try:
                conn2.execute("DELETE FROM calendar_events WHERE event_id = ?", (old_id,))
                _upsert_event(conn2, result)
                conn2.commit()
            finally:
                conn2.close()

            pushed += 1
        except Exception as e:
            log.warning("Failed to push local event %s: %s", old_id, e)

    return pushed


def update_event(event_id: str, end_dt: str) -> None:
    """Update event end time locally and in Google Calendar if applicable."""
    conn = open_rw()
    try:
        row = conn.execute(
            "SELECT * FROM calendar_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if not row:
            return
        ev = dict(row)
        conn.execute(
            "UPDATE calendar_events SET end_dt = ? WHERE event_id = ?",
            (end_dt, event_id),
        )
        conn.commit()
    finally:
        conn.close()

    if not ev.get("local_only"):
        creds = _get_credentials()
        if creds:
            try:
                from googleapiclient.discovery import build
                service = build("calendar", "v3", credentials=creds, cache_discovery=False)
                g_ev = service.events().get(calendarId="primary", eventId=event_id).execute()
                g_ev["end"] = {"dateTime": end_dt, "timeZone": "Europe/Paris"}
                service.events().update(
                    calendarId="primary", eventId=event_id, body=g_ev
                ).execute()
            except Exception as e:
                log.warning("Failed to update Google event %s: %s", event_id, e)




def _google_service_for(ev: dict):
    """The Google service for a cached event that lives on Google. Raises GoogleNotConnected without credentials."""
    creds = _get_credentials()
    if creds is None:
        raise GoogleNotConnected()
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _read_row(event_id: str) -> dict:
    conn = open_rw()
    try:
        row = conn.execute("SELECT * FROM calendar_events WHERE event_id = ?", (event_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise EventNotFound(event_id)
    return dict(row)


def delete_event(event_id: str) -> None:
    """Delete an event on Google first, then in the cache. A Google event is never dropped from the
    cache unless Google answered. Raises EventNotFound, GoogleNotConnected or GoogleRefused."""
    ev = _read_row(event_id)
    if not ev.get("local_only"):
        service = _google_service_for(ev)
        try:
            service.events().delete(calendarId="primary", eventId=event_id).execute()
        except HttpError as exc:
            raise GoogleRefused("delete", exc.reason) from exc
    conn = open_rw()
    try:
        conn.execute("DELETE FROM calendar_events WHERE event_id = ?", (event_id,))
        conn.commit()
    finally:
        conn.close()


def move_event(event_id: str, start_dt: str, end_dt: str) -> None:
    """Move an event on Google first, then in the cache. An all-day event moves as dates. Raises
    EventNotFound, GoogleNotConnected or GoogleRefused."""
    ev = _read_row(event_id)
    all_day = bool(ev.get("all_day"))
    if all_day:
        start_dt, end_dt = start_dt[:10], end_dt[:10]
    if not ev.get("local_only"):
        service = _google_service_for(ev)
        try:
            g_ev = service.events().get(calendarId="primary", eventId=event_id).execute()
            if all_day:
                g_ev["start"] = {"date": start_dt}
                g_ev["end"] = {"date": end_dt}
            else:
                g_ev["start"] = {"dateTime": start_dt, "timeZone": "Europe/Paris"}
                g_ev["end"] = {"dateTime": end_dt, "timeZone": "Europe/Paris"}
            service.events().update(calendarId="primary", eventId=event_id, body=g_ev).execute()
        except HttpError as exc:
            raise GoogleRefused("move", exc.reason) from exc
    conn = open_rw()
    try:
        conn.execute(
            "UPDATE calendar_events SET start_dt = ?, end_dt = ? WHERE event_id = ?",
            (start_dt, end_dt, event_id),
        )
        conn.commit()
    finally:
        conn.close()


# ── user_prefs helpers ─────────────────────────────────────────────────────────

def _get_pref(key: str) -> str | None:
    conn = open_rw()
    try:
        row = conn.execute(
            "SELECT value FROM user_prefs WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _set_pref(key: str, value: str) -> None:
    conn = open_rw()
    try:
        conn.execute(
            """INSERT INTO user_prefs (key, value, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT (key) DO UPDATE SET
                   value = excluded.value, updated_at = excluded.updated_at""",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def _del_pref(key: str) -> None:
    conn = open_rw()
    try:
        conn.execute("DELETE FROM user_prefs WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()
