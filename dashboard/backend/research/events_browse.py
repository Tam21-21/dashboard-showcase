"""The Events tab reads of the Data library (lane UI38-0, DECISIONS #472).

Three reads over the active rows of research_events: a search across event types by words, ticker and
analyst action; the tickers most in the news; the analyst actions by count. No new table or index:
each read walks ix_events_type_knowledge by type and filters the payload as a residual.
Every user value is a bound parameter; the SQL text is assembled from fixed fragments only.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from typing import Any

from dashboard.backend.events.tickers import normalize_ticker
from dashboard.backend.events.types import earnings as earnings_type

SEARCH_TYPES = ("news", "analyst_rating", "sec_13d")

# Payload fields the words search looks in, per type (events/types/*.py).
_Q_FIELDS = {
    "news": ("headline",),
    "analyst_rating": ("analyst_firm", "action_company", "new_grade", "old_grade", "raw_ticker"),
    "sec_13d": ("filer_name", "subject_name"),
}

# The tickers of a 13D: the map rows of its primary issuer (one cik, SEC company-tickers source, still current).
_MAP_JOIN = (
    "research_event_companies c JOIN research_cik_ticker_map m ON m.cik = c.cik "
    "WHERE c.role = 'primary_issuer' AND m.source = 'sec_company_tickers' AND m.effective_to IS NULL"
)


class BadTypes(ValueError):
    """The `types` list is empty or names an unknown event type (a 422 naming it)."""


class MalformedCursor(ValueError):
    """A client-supplied after_cursor that does not parse (a 400, not a 500)."""


def parse_types(raw: str) -> list[str]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise BadTypes("types is empty; give a comma list of news, analyst_rating, sec_13d")
    bad = [p for p in parts if p not in SEARCH_TYPES]
    if bad:
        raise BadTypes(f"unknown event type {', '.join(bad)}; allowed: {', '.join(SEARCH_TYPES)}")
    return list(dict.fromkeys(parts))


def _type_clause(event_type: str, q: str | None, ticker: str | None,
                 action: str | None, params: list[Any]) -> str | None:
    """One OR-branch for one type, or None when a filter rules the whole type out."""
    if action is not None and event_type != "analyst_rating":
        return None
    parts = ["e.event_type = ?"]
    params.append(event_type)
    if q:
        likes = []
        for field in _Q_FIELDS[event_type]:
            likes.append(f"instr(lower(coalesce(json_extract(e.payload, '$.{field}'), '')), lower(?)) > 0")
            params.append(q)
        parts.append("(" + " OR ".join(likes) + ")")
    if action is not None:
        parts.append("json_extract(e.payload, '$.action_company') = ?")
        params.append(action)
    if ticker:
        if event_type == "news":
            parts.append("EXISTS (SELECT 1 FROM json_each(e.payload, '$.symbols') j WHERE j.value = ?)")
            params.append(ticker.upper())
        elif event_type == "analyst_rating":
            parts.append("json_extract(e.payload, '$.raw_ticker') = ?")
            params.append(ticker.upper())
        else:
            parts.append(f"EXISTS (SELECT 1 FROM {_MAP_JOIN} AND c.event_id = e.event_id AND m.ticker = ?)")
            params.append(normalize_ticker(ticker))
    return "(" + " AND ".join(parts) + ")"


def _where(types: list[str], q: str | None, ticker: str | None,
           action: str | None) -> tuple[str, list[Any]]:
    params: list[Any] = []
    branches = [b for t in types if (b := _type_clause(t, q, ticker, action, params)) is not None]
    # _type_clause appends params only for kept branches, so the order below matches the text.
    if not branches:
        return "e.status = 'active' AND 0", []
    return "e.status = 'active' AND (" + " OR ".join(branches) + ")", params


def _tickers(conn: sqlite3.Connection, event_type: str, event_id: int, payload: dict) -> list[str]:
    if event_type == "news":
        return list(payload.get("symbols") or [])
    if event_type == "analyst_rating":
        t = payload.get("raw_ticker")
        return [t] if t else []
    rows = conn.execute(
        f"SELECT DISTINCT m.ticker FROM {_MAP_JOIN} AND c.event_id = ? ORDER BY m.ticker", (event_id,)
    ).fetchall()
    return [r[0] for r in rows]


def search_events(conn: sqlite3.Connection, types: list[str], q: str | None, ticker: str | None,
                  action: str | None, after_cursor: str | None, limit: int) -> dict:
    where, params = _where(types, q or None, ticker or None, action or None)
    total = conn.execute(f"SELECT COUNT(*) FROM research_events e WHERE {where}", params).fetchone()[0]
    page_where, page_params = where, list(params)
    if after_cursor:
        try:
            ck, ce = after_cursor.split("_", 1)  # '<knowledge_at>_<event_id>'
            ck, ce = int(ck), int(ce)
        except ValueError as exc:
            raise MalformedCursor(f"malformed after_cursor {after_cursor!r}: {exc}") from exc
        page_where += " AND (e.knowledge_at < ? OR (e.knowledge_at = ? AND e.event_id < ?))"
        page_params += [ck, ck, ce]
    db_rows = conn.execute(
        "SELECT e.event_id, e.event_type, e.knowledge_at, e.occurred_at, e.source, e.payload "
        f"FROM research_events e WHERE {page_where} "
        "ORDER BY e.knowledge_at DESC, e.event_id DESC LIMIT ?",
        (*page_params, limit)).fetchall()
    events = []
    for r in db_rows:
        payload = json.loads(r["payload"])  # fails loud on a malformed payload
        events.append({
            "event_id": r["event_id"], "event_type": r["event_type"],
            "knowledge_at": r["knowledge_at"], "occurred_at": r["occurred_at"],
            "source": r["source"], "tickers": _tickers(conn, r["event_type"], r["event_id"], payload),
            "payload": payload,
        })
    nxt = None
    if len(events) == limit:
        last = events[-1]
        nxt = {"after_cursor": f"{last['knowledge_at']}_{last['event_id']}"}
    return {"events": events, "next": nxt, "total_matching": total}


def news_top(conn: sqlite3.Connection, now: int, days: int, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT ticker, COUNT(*) AS n FROM ("
        "  SELECT DISTINCT e.event_id AS event_id, j.value AS ticker "
        "  FROM research_events e, json_each(e.payload, '$.symbols') j "
        "  WHERE e.status = 'active' AND e.event_type = 'news' AND e.knowledge_at >= ? "
        "    AND j.type = 'text' AND j.value <> '') "
        "GROUP BY ticker ORDER BY n DESC, ticker ASC LIMIT ?",
        (now - days * 86400, limit)).fetchall()
    return [{"ticker": r["ticker"], "count": r["n"]} for r in rows]


def rating_actions(conn: sqlite3.Connection) -> dict:
    total = conn.execute(
        "SELECT COUNT(*) FROM research_events WHERE status = 'active' AND event_type = 'analyst_rating'"
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT json_extract(payload, '$.action_company') AS action, COUNT(*) AS n "
        "FROM research_events WHERE status = 'active' AND event_type = 'analyst_rating' "
        "AND json_extract(payload, '$.action_company') IS NOT NULL "
        "GROUP BY action ORDER BY n DESC, action ASC").fetchall()
    return {"total": total, "actions": [{"action": r["action"], "count": r["n"]} for r in rows]}


# ---- Earnings (lane EA3-0): releases, surprise, price reaction -------------------------------

# A later update for the same ticker and quarter folds into the first row within this window.
_UPDATE_WINDOW_S = 30 * 86400
_FIGURES = ("adjusted", "eps_actual", "eps_estimate", "sales_actual", "sales_estimate",
            "eps_word", "sales_word", "headline")


def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _surprise(actual: float, estimate: float) -> float | None:
    if estimate == 0:
        return None
    return round((actual - estimate) / abs(estimate), 4)


def _company(headline: str) -> str:
    """The headline's text before ' Q<n>' (no name table exists; planner ruling 2026-10-23)."""
    head = re.sub(r"^(Update|Correction|CORRECTION): ", "", headline)
    return re.split(r" Q[1-4]\b", head, maxsplit=1)[0]


def _earnings_row(p: dict) -> dict:
    return {
        "ticker": p["raw_ticker"], "company": _company(p["headline"]),
        "quarter": f"Q{p['fiscal_quarter']}", "release_at": _iso(p["release_at"]),
        "adjusted": p["adjusted"],
        "eps_actual": p["eps_actual"], "eps_estimate": p["eps_estimate"],
        "eps_surprise": _surprise(p["eps_actual"], p["eps_estimate"]),
        "sales_actual": p["sales_actual"], "sales_estimate": p["sales_estimate"],
        "sales_surprise": _surprise(p["sales_actual"], p["sales_estimate"]),
        "result": p["eps_word"], "headline": p["headline"],
        "corrected": False, "first_printed": None, "corrected_at": None,
    }


def earnings(conn: sqlite3.Connection, limit: int) -> dict:
    """The newest `limit` earnings releases with their price reaction.

    Raises earnings_reaction.ClosesUnavailable when Alpaca cannot answer (never rows without closes).
    """
    from dashboard.backend.research import earnings_reaction

    unread = 0
    firsts: list[dict] = []
    updates: list[dict] = []
    sql = ("SELECT payload FROM research_events WHERE status = 'active' AND event_type = 'earnings' "
           "ORDER BY knowledge_at DESC, event_id DESC")
    for r in conn.execute(sql).fetchall():
        p = json.loads(r["payload"])
        if any(k not in p for k in earnings_type.REQUIRED_FIELDS):
            unread += 1
            continue
        (updates if p["is_update"] else firsts).append(p)
    rows: list[tuple[int, dict]] = []
    folded: set[int] = set()
    for p in firsts:
        row = _earnings_row(p)
        later = [u for u in updates
                 if (u["raw_ticker"], u["fiscal_quarter"]) == (p["raw_ticker"], p["fiscal_quarter"])
                 and p["release_at"] < u["release_at"] <= p["release_at"] + _UPDATE_WINDOW_S]
        if later:
            folded.update(id(u) for u in later)
            u = max(later, key=lambda x: x["release_at"])
            row = _earnings_row({**p, **{k: u[k] for k in _FIGURES}})
            row["release_at"] = _iso(p["release_at"])
            row["corrected"] = True
            row["corrected_at"] = _iso(u["release_at"])
            row["first_printed"] = {k: p[k] for k in _FIGURES}
        rows.append((p["release_at"], row))
    for u in updates:                       # an update with no first row stands as its own row
        if id(u) not in folded:
            rows.append((u["release_at"], _earnings_row(u)))
    rows.sort(key=lambda x: x[0], reverse=True)
    picked = rows[:limit]
    out = [r for _, r in picked]
    if out:
        earliest = dt.datetime.strptime(min(r["release_at"] for r in out), "%Y-%m-%dT%H:%M:%SZ").date()
        start = earliest - dt.timedelta(days=earnings_reaction.LEAD_DAYS)
        end = dt.datetime.now(dt.timezone.utc).date()
        closes = earnings_reaction.fetch_daily_closes([r["ticker"] for r in out], start, end)
        for (release, _), row in zip(picked, out):
            win = earnings_reaction.reaction_window(release, closes.get(row["ticker"], []))
            row["reaction"] = win.pop("reaction")
            row["closes"] = win
    return {"rows": out, "unread": unread}
