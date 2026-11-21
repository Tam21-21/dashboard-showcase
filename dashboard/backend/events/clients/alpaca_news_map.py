"""Map a raw Alpaca news article to a canonical `news` event RECORD (F7). Pure (no I/O).

Mirrors benzinga_map.map_rating, minus ticker->CIK resolution: news carries NO company links
(companies=[]), so the symbol set lives in the payload and a multi-symbol / crypto article never
trips the resolution or the correction-time company-link guard (F7 arch §4).

Field mapping (verified against the live Alpaca shape 2026-06-17):
  id          -> producer_id / natural_key (str of the int; the stable dedup key)
  created_at  -> knowledge_at AND occurred_at (the publish instant, RFC-3339 UTC)
  updated_at  -> payload.updated_at (the revision signal a re-fetch compares on)
  headline    -> payload.headline (HTML entities decoded)
  summary     -> payload.summary (decoded; key omitted when blank)
  author      -> payload.author (key omitted when blank)
  url         -> payload.url (the click-through)
  source      -> payload.news_source (the article's own source, e.g. "benzinga")
  symbols     -> payload.symbols (list[str]; multi-symbol, crypto un-slashed)
The full raw article rides research_events.raw_input as a JSON string (audit / re-map).
The article body (`content`) is NEVER stored (headlines-keep-all posture).
"""
from __future__ import annotations

import datetime as _dt
import html
import json
from typing import Optional

from ..types import news as news_type     # PAYLOAD_SCHEMA_VERSION


class AlpacaNewsUnmappable(ValueError):
    """A raw Alpaca article lacks a field required to build a canonical record (-> quarantine)."""


def _rfc3339_to_epoch(value: object, field: str) -> int:
    """Parse an RFC-3339 UTC instant (e.g. '2026-06-17T09:43:39Z') to epoch seconds (int)."""
    if value is None:
        raise AlpacaNewsUnmappable(f"missing required Alpaca news field: {field!r}")
    s = str(value).strip()
    if s == "":
        raise AlpacaNewsUnmappable(f"blank Alpaca news field: {field!r}")
    # fromisoformat handles the '+00:00' form; normalize a trailing 'Z' to it first.
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = _dt.datetime.fromisoformat(iso)
    except ValueError as exc:
        raise AlpacaNewsUnmappable(f"{field!r} not an RFC-3339 instant: {value!r}") from exc
    if dt.tzinfo is None:                                       # treat a naive instant as UTC
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp())


def _decode(value: object) -> str:
    """Decode HTML entities (&#39; &#8216; …) to characters for display."""
    return html.unescape(str(value))


def _opt_decoded(value: object) -> Optional[str]:
    """A decoded non-empty string, or None (so a blank source field becomes an absent key)."""
    if value is None:
        return None
    s = str(value).strip()
    return _decode(s) if s else None


def map_article(raw: dict) -> dict:
    """Build a canonical `news` RECORD dict from a raw Alpaca news article.

    Raises AlpacaNewsUnmappable when a required field is missing/blank. companies is always
    empty (no CIK links — symbols live in the payload).
    """
    def req(key: str) -> object:
        val = raw.get(key)
        if val is None or (isinstance(val, str) and val.strip() == ""):
            raise AlpacaNewsUnmappable(f"missing required Alpaca news field: {key!r}")
        return val

    raw_id = req("id")                                          # the stable dedup key (int)
    try:
        article_id = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise AlpacaNewsUnmappable(f"news id not an int: {raw_id!r}") from exc

    headline = _decode(req("headline"))
    url = str(req("url")).strip()
    created_epoch = _rfc3339_to_epoch(raw.get("created_at"), "created_at")
    updated_epoch = _rfc3339_to_epoch(raw.get("updated_at"), "updated_at")
    news_source = str(req("source")).strip()                   # the article's own source; required, never defaulted (a blank source quarantines rather than fabricating provenance)

    symbols_raw = raw.get("symbols") or []                     # multi-symbol; may be empty
    if not isinstance(symbols_raw, list):
        raise AlpacaNewsUnmappable(f"news symbols not a list: {symbols_raw!r}")
    # strip blanks AND de-duplicate (preserve order): a repeated ticker from the source would
    # otherwise be stored twice and collide on the React key in the feed.
    symbols = list(dict.fromkeys(s.strip() for s in (str(x) for x in symbols_raw) if s.strip()))

    payload: dict = {
        "article_id": article_id,
        "headline": headline,
        "created_at": created_epoch,
        "updated_at": updated_epoch,
        "url": url,
        "news_source": news_source,
        "symbols": symbols,
    }
    summary = _opt_decoded(raw.get("summary"))                 # OPTIONAL: add the key only when present
    if summary is not None:
        payload["summary"] = summary
    author = _opt_decoded(raw.get("author"))
    if author is not None:
        payload["author"] = author

    return {
        "event_type": "news",
        "natural_key": str(article_id),
        "producer_id": str(article_id),
        "knowledge_at": created_epoch,
        "occurred_at": created_epoch,                          # news occurs when published
        "source": "alpaca_news",                              # the ROW-level integration value
        "origin": "fetched",
        "payload_schema_version": news_type.PAYLOAD_SCHEMA_VERSION,
        "raw_input": json.dumps(raw, sort_keys=True),
        "payload": payload,
        "companies": [],                                      # F7: no CIK links; symbols in payload
    }
