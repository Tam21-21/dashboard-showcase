"""Gmail read-only access using the shared Google OAuth credentials.

Mirrors calendar_sync's pattern. Same client_id / client_secret env vars, same
google_oauth_tokens row in user_prefs, just a different Google API service.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from email.utils import parseaddr, parsedate_to_datetime

import bleach

from .calendar_sync import SCOPES, _get_pref, _set_pref, _del_pref

log = logging.getLogger("dashboard.gmail")

# Either of these scopes grants read access to messages
_GMAIL_READ_SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
)
_GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


# Bleach allowlist tuned for email HTML.
# Goal: render formatted email content safely inside a sandboxed iframe — no
# scripts, no iframes-within-iframes, no event handlers, no remote-data URIs in
# unexpected places. CSS classes are stripped because we render in an isolated
# iframe with a clean stylesheet, so class-based styling would be dead weight.
_ALLOWED_TAGS = {
    "a", "abbr", "address", "b", "blockquote", "br", "caption", "code", "col",
    "colgroup", "div", "em", "fieldset", "figure", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "i", "img", "label", "legend", "li", "ol", "p", "pre",
    "q", "s", "small", "span", "strike", "strong", "sub", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul", "font", "center",
}

_ALLOWED_ATTRS = {
    "*": ["style", "align", "valign", "bgcolor", "width", "height",
          "cellpadding", "cellspacing", "border", "title", "dir"],
    "a": ["href", "rel", "target"],
    "img": ["src", "alt", "width", "height"],
    "table": ["border", "cellpadding", "cellspacing", "width", "bgcolor"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
    "font": ["color", "face", "size"],
}

_ALLOWED_PROTOCOLS = ["http", "https", "mailto", "tel", "cid", "data"]


def _build_service():
    """Return a Gmail API service object using shared OAuth credentials."""
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

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
            log.warning("Google token revoked, clearing stored credentials: %s", e)
            _del_pref("google_oauth_tokens")
            return None
        except Exception as e:
            log.warning("Google credential refresh failed: %s", e)
            return None

    if not creds.valid:
        return None

    # Need at least one Gmail read scope to call the API
    granted = set(creds.scopes or [])
    if not any(s in granted for s in _GMAIL_READ_SCOPES):
        return None

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def has_gmail_scope() -> bool:
    """True if stored credentials include any Gmail read scope (readonly or modify)."""
    raw = _get_pref("google_oauth_tokens")
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except Exception:
        return False
    granted = set(data.get("scopes") or [])
    return any(s in granted for s in _GMAIL_READ_SCOPES)


def can_modify() -> bool:
    """True only if stored credentials include gmail.modify (label changes)."""
    raw = _get_pref("google_oauth_tokens")
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except Exception:
        return False
    return _GMAIL_MODIFY_SCOPE in (data.get("scopes") or [])


# ── Listing ────────────────────────────────────────────────────────────────────

def list_recent_messages(max_results: int = 15) -> list[dict]:
    """Return the most recent INBOX messages with header-only metadata.

    Each item: {id, thread_id, from_name, from_email, subject, snippet,
                date_iso, is_unread}
    """
    service = _build_service()
    if service is None:
        return []

    # INBOX label filters out Promotions/Social/Updates by default — those land
    # under CATEGORY_PROMOTIONS / CATEGORY_SOCIAL / CATEGORY_UPDATES, which are
    # technically present in the inbox UI but not in the INBOX label.
    listed = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
        .execute()
    )
    ids = [m["id"] for m in listed.get("messages", [])]
    if not ids:
        return []

    out: list[dict] = []
    for mid in ids:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=mid,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        out.append(_summarize(msg))
    return out


def _summarize(msg: dict) -> dict:
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    from_raw = headers.get("from", "")
    name, addr = parseaddr(from_raw)
    date_raw = headers.get("date", "")
    try:
        date_iso = parsedate_to_datetime(date_raw).isoformat() if date_raw else ""
    except Exception:
        date_iso = ""
    label_ids = set(msg.get("labelIds", []))
    return {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "from_name": name or addr or "(unknown)",
        "from_email": addr,
        "subject": headers.get("subject", "(no subject)"),
        "snippet": msg.get("snippet", ""),
        "date_iso": date_iso,
        "is_unread": "UNREAD" in label_ids,
    }


# ── Single message + sanitization ──────────────────────────────────────────────

def get_message(message_id: str, *, allow_images: bool = False) -> dict:
    """Return {headers, html_safe, has_images_blocked}.

    Side effect: if the message has the UNREAD label and we hold gmail.modify
    scope, the label is removed as part of this call. The unread-removal is
    best-effort — failure is logged but doesn't break the fetch.
    """
    service = _build_service()
    if service is None:
        raise RuntimeError("Gmail not connected")

    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )

    if "UNREAD" in (msg.get("labelIds") or []) and can_modify():
        try:
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
        except Exception as e:
            log.warning("Could not mark %s as read: %s", message_id, e)

    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    from_raw = headers.get("from", "")
    to_raw = headers.get("to", "")
    name, addr = parseaddr(from_raw)
    date_raw = headers.get("date", "")
    try:
        date_iso = parsedate_to_datetime(date_raw).isoformat() if date_raw else ""
    except Exception:
        date_iso = ""

    html_body, plain_body = _extract_bodies(msg.get("payload", {}))
    raw_html = html_body if html_body else f"<pre>{bleach.clean(plain_body or '', tags=set(), strip=True)}</pre>"

    safe_html, blocked = _sanitize(raw_html, allow_images=allow_images)

    return {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "from_name": name or addr or "(unknown)",
        "from_email": addr,
        "to": to_raw,
        "subject": headers.get("subject", "(no subject)"),
        "date_iso": date_iso,
        "html_safe": safe_html,
        "has_images_blocked": blocked,
    }


def _extract_bodies(payload: dict) -> tuple[str, str]:
    """Walk the MIME tree and pull out (html, plain) bodies."""
    html_body = ""
    plain_body = ""

    def walk(part: dict) -> None:
        nonlocal html_body, plain_body
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data:
            try:
                decoded = base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")
            except Exception:
                decoded = ""
            if mime == "text/html" and not html_body:
                html_body = decoded
            elif mime == "text/plain" and not plain_body:
                plain_body = decoded
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return html_body, plain_body


# Drop whole blocks of tags that leak text content (their inner CSS/JS/metadata
# would otherwise be rendered as visible text by bleach with strip=True).
_STRIP_BLOCK_RE = re.compile(
    r"<(style|script|head|title|noscript)\b[^>]*>.*?</\1\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
# Void elements with no text content but that bleach would drop anyway —
# stripping here too keeps the output cleaner.
_STRIP_VOID_RE = re.compile(
    r"<(meta|link|base)\b[^>]*/?>",
    flags=re.IGNORECASE,
)


def _pre_strip(html: str) -> str:
    html = _STRIP_BLOCK_RE.sub("", html)
    html = _STRIP_VOID_RE.sub("", html)
    return html


def _sanitize(raw_html: str, *, allow_images: bool) -> tuple[str, bool]:
    """Bleach-clean the HTML. Returns (safe_html, images_were_blocked)."""
    raw_html = _pre_strip(raw_html)
    images_blocked = False

    if not allow_images:
        # Strip <img src> by removing the src attr — keeps layout (alt text shows)
        # but blocks the network fetch. Cheap and effective; no DOM parse needed.
        if "<img" in raw_html.lower():
            images_blocked = True
        attrs = dict(_ALLOWED_ATTRS)
        attrs["img"] = [a for a in attrs["img"] if a != "src"]
    else:
        attrs = _ALLOWED_ATTRS

    cleaned = bleach.clean(
        raw_html,
        tags=_ALLOWED_TAGS,
        attributes=attrs,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )
    return cleaned, images_blocked
