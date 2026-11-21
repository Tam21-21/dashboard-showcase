"""Evening notification (lanes ES1b1 and ES1b2): the push message, its encryption and its signature,
and (ES1b2) the stored subscriptions and the sending.
References: RFC 8291 (aes128gcm encryption, section 5 and Appendix A), RFC 8292 (VAPID header),
WebKit "Meet Declarative Web Push" (a message with "web_push": 8030 and a "notification" is shown by the
system with no service worker). The settings are read from the environment at call time and never logged.
"""
from __future__ import annotations

import base64
import json
import os
import logging
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TITLE = "Evening Summary"
TAIL = "Open the app for all the details."
NOT_SET = "The evening notification is not set up on the server yet."
RECORD_SIZE = 4096
VAPID_LIFETIME_SECONDS = 43200
DECLARATIVE_WEB_PUSH = 8030
# Four hours, after which an evening line is stale (RFC 8030 requires a TTL header).
TTL_SECONDS = 14400
TIMEOUT_SECONDS = 20
BAD_ADDRESS = "That is not a push address."
BAD_KEYS = "The phone sent keys this server cannot use."
UNUSABLE_KEYS = "This phone's keys cannot be used."

log = logging.getLogger(__name__)


class PushError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _not_set() -> PushError:
    return PushError(503, NOT_SET)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _point(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _private_key() -> ec.EllipticCurvePrivateKey:
    text = os.environ.get("PUSH_VAPID_PRIVATE_KEY", "").strip()
    if not text:
        raise _not_set()
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError as exc:
        raise _not_set() from exc
    if len(raw) != 32:
        raise _not_set()
    try:
        return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    except ValueError as exc:
        raise _not_set() from exc


def _subject() -> str:
    text = os.environ.get("PUSH_VAPID_SUBJECT", "").strip()
    if not (text.startswith("mailto:") or text.startswith("https://")):
        raise _not_set()
    return text


def _open_url() -> str:
    text = os.environ.get("PUSH_OPEN_URL", "").strip()
    if not text.startswith("https://"):
        raise _not_set()
    return text


def _settings() -> None:
    """Check all three settings; ES1b2 calls this before a send."""
    _private_key()
    _subject()
    _open_url()


def public_key() -> str:
    return _b64(_point(_private_key()))


def _hkdf(length: int, salt: bytes, info: bytes, secret: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(secret)


def encrypt(
    plaintext: bytes,
    ua_public: bytes,
    auth_secret: bytes,
    salt: Optional[bytes] = None,
    sender_key: Optional[ec.EllipticCurvePrivateKey] = None,
) -> bytes:
    """RFC 8291 aes128gcm body for one push message."""
    if salt is None:
        salt = secrets.token_bytes(16)
    if sender_key is None:
        sender_key = ec.generate_private_key(ec.SECP256R1())
    sender_public = _point(sender_key)
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = sender_key.exchange(ec.ECDH(), receiver)
    ikm = _hkdf(32, auth_secret, b"WebPush: info\x00" + ua_public + sender_public, shared)
    cek = _hkdf(16, salt, b"Content-Encoding: aes128gcm\x00", ikm)
    nonce = _hkdf(12, salt, b"Content-Encoding: nonce\x00", ikm)
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    return salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(sender_public)]) + sender_public + ciphertext


def vapid_header(endpoint: str, now: Optional[datetime] = None) -> str:
    """RFC 8292 Authorization value: `vapid t=<jwt>, k=<public key>`."""
    key = _private_key()
    subject = _subject()
    parts = urlsplit(endpoint)
    if not parts.scheme or not parts.netloc:
        raise PushError(502, "The push address of the phone is not a web address.")
    when = now if now is not None else datetime.now(timezone.utc)
    header = {"typ": "JWT", "alg": "ES256"}
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int(when.timestamp()) + VAPID_LIFETIME_SECONDS,
        "sub": subject,
    }

    def part(obj: dict) -> str:
        return _b64(json.dumps(obj, separators=(",", ":")).encode("utf-8"))

    signing_input = f"{part(header)}.{part(claims)}"
    der = key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"vapid t={signing_input}.{_b64(signature)}, k={_b64(_point(key))}"


def message(body: str) -> bytes:
    """The declarative web push message: the system shows it, a tap opens the app address."""
    payload = {
        "web_push": DECLARATIVE_WEB_PUSH,
        "notification": {"title": TITLE, "body": body, "navigate": _open_url()},
    }
    return json.dumps(payload).encode("utf-8")


def line(budget: Optional[bool], steps: Optional[bool]) -> str:
    """The body line, word for word the mockup's lineOf. True is on track, False off track, None not read."""
    b = None if budget is None else ("on track with your budget" if budget else "over on your budget")
    s = None if steps is None else ("on track with your steps" if steps else "short on your steps")

    def cap(text: str) -> str:
        return text[:1].upper() + text[1:]

    if b is not None and s is not None:
        if budget and steps:
            lead = "On track with your budget and your steps."
        elif not budget and not steps:
            lead = "Over on your budget and short on your steps."
        else:
            lead = f"{cap(b if budget else s)}, {s if budget else b}."
    elif b is not None:
        lead = f"{cap(b)}. Your watch has not synced."
    elif s is not None:
        lead = f"{cap(s)}. The bank was not read."
    else:
        lead = "Nothing could be read tonight."
    return f"{lead} {TAIL}"


# ── Subscriptions and sending (lane ES1b2) ─────────────────────────────────────

def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS)


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _decode_keys(p256dh: str, auth: str) -> tuple[bytes, bytes]:
    """The phone's point (65 bytes, on the curve) and auth secret (16 bytes), or ValueError."""
    point = _unb64(p256dh)
    if len(point) != 65:
        raise ValueError("p256dh is not 65 bytes")
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    secret = _unb64(auth)
    if len(secret) != 16:
        raise ValueError("auth is not 16 bytes")
    return point, secret


def _stamp(now: Optional[datetime]) -> str:
    when = now if now is not None else datetime.now(timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def subscribe(conn: sqlite3.Connection, endpoint: str, p256dh: str, auth: str,
              now: Optional[datetime] = None) -> None:
    if not endpoint.startswith("https://") or not urlsplit(endpoint).netloc:
        raise PushError(422, BAD_ADDRESS)
    try:
        _decode_keys(p256dh, auth)
    except ValueError as exc:
        raise PushError(422, BAD_KEYS) from exc
    conn.execute(
        "INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth",
        (endpoint, p256dh, auth, _stamp(now)),
    )
    conn.commit()


def unsubscribe(conn: sqlite3.Connection, endpoint: str) -> None:
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    conn.commit()


def known(conn: sqlite3.Connection, endpoint: str) -> bool:
    return conn.execute("SELECT 1 FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone() is not None


def _fail(conn: sqlite3.Connection, endpoint: str, reason: str) -> None:
    conn.execute("UPDATE push_subscriptions SET last_error = ? WHERE endpoint = ?", (reason, endpoint))


def _answer_reason(resp: httpx.Response) -> Optional[str]:
    """The push service's own word ("reason" in a JSON answer), or None when there is none to keep."""
    try:
        data = json.loads(resp.content)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    reason = data.get("reason")
    if isinstance(reason, str) and 1 <= len(reason) <= 80:
        return reason
    return None


def send_all(conn: sqlite3.Connection, body: str, now: Optional[datetime] = None) -> dict:
    """One message to every stored phone. Raises PushError(503) when the server has no keys."""
    _settings()
    stamp = _stamp(now)
    plaintext = message(body)
    counts = {"sent": 0, "gone": 0, "failed": 0}
    rows = conn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()
    with _client() as client:
        for endpoint, p256dh, auth in ((r[0], r[1], r[2]) for r in rows):
            try:
                point, secret = _decode_keys(p256dh, auth)
                payload = encrypt(plaintext, point, secret)
                headers = {
                    "TTL": str(TTL_SECONDS),
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                    "Authorization": vapid_header(endpoint),
                }
            except ValueError:
                _fail(conn, endpoint, UNUSABLE_KEYS)
                counts["failed"] += 1
                continue
            except PushError as exc:
                _fail(conn, endpoint, exc.detail)
                counts["failed"] += 1
                continue
            try:
                resp = client.post(endpoint, content=payload, headers=headers)
            except httpx.TimeoutException:
                _fail(conn, endpoint, f"The push service did not answer in {TIMEOUT_SECONDS} seconds.")
                counts["failed"] += 1
                continue
            except httpx.HTTPError:
                _fail(conn, endpoint, "The push service could not be reached.")
                counts["failed"] += 1
                continue
            status = resp.status_code
            if 200 <= status <= 204:
                conn.execute("UPDATE push_subscriptions SET last_sent_at = ?, last_error = NULL WHERE endpoint = ?",
                             (stamp, endpoint))
                counts["sent"] += 1
            elif status in (404, 410):
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
                counts["gone"] += 1
            else:
                reason = _answer_reason(resp)
                _fail(conn, endpoint,
                      f"The push service answered {status}: {reason}." if reason else f"The push service answered {status}.")
                counts["failed"] += 1
    conn.commit()
    return counts


def send_for(days: list[str], now: Optional[datetime] = None) -> dict:
    """The 22:00 loop's send: only when today's (Paris) Summary was stored by this very look."""
    from . import evening_summary, userdb

    zero = {"sent": 0, "gone": 0, "failed": 0}
    when = datetime.now(timezone.utc) if now is None else now
    if when.astimezone(ZoneInfo("Europe/Paris")).date().isoformat() not in days:
        return zero
    try:
        _settings()
    except PushError:
        log.info("evening notification not set up, nothing sent")
        return zero
    conn = userdb.open_rw()
    try:
        summary = evening_summary.newest(conn)
        if summary is None:
            return zero
        verdict = summary["verdict"]
        return send_all(conn, line(verdict["budget"], verdict["steps"]), now)
    finally:
        conn.close()
