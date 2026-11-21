"""The money store (lane MY1): what Finary reports, stored whole, and the reads the pages draw.

A scheduled Claude routine reads Finary and posts one snapshot, twice a day. `ingest` validates the
snapshot and stores it whole in one transaction. A snapshot is never edited afterwards. The reads
(`overview`, `budget`) answer from the newest snapshot. `budget_targets` is the dashboard's own record
of the monthly targets Tamer sets; Finary never sees it. The tables are created by userdb.init_db.

All amounts are euros. Amounts are rounded to cents only on the way out.
"""
from __future__ import annotations

import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")

STALE_AFTER = timedelta(hours=14)  # money plan: a snapshot older than this is stale (two syncs a day)
WARN_AHEAD = 0.10  # DECISIONS #472 item 5: warn when spending is more than 10% ahead of pace
_SLUG = re.compile(r"^[a-z_]+$")
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class MoneyInputError(ValueError):
    """The posted figures or the typed target are not acceptable. The message is shown as is."""


class TargetNotFound(LookupError):
    """No budget target is set for that category."""


# ---------------------------------------------------------------- validation helpers

def _need(obj: Any, key: str, where: str) -> Any:
    if not isinstance(obj, dict) or key not in obj or obj[key] is None:
        raise MoneyInputError(f"The snapshot is missing {where}{key}.")
    return obj[key]


def _text(obj: Any, key: str, where: str) -> str:
    value = _need(obj, key, where)
    if not isinstance(value, str) or not value:
        raise MoneyInputError(f"{where}{key} must be text.")
    return value


def _opt_text(obj: dict, key: str, where: str) -> Optional[str]:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MoneyInputError(f"{where}{key} must be text.")
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _num(obj: Any, key: str, where: str) -> float:
    value = _need(obj, key, where)
    if not _is_number(value):
        raise MoneyInputError(f"{where}{key} must be a number.")
    return float(value)


def _opt_num(obj: dict, key: str, where: str) -> Optional[float]:
    value = obj.get(key)
    if value is None:
        return None
    if not _is_number(value):
        raise MoneyInputError(f"{where}{key} must be a number.")
    return float(value)


def _int(obj: Any, key: str, where: str, minimum: int = 0) -> int:
    value = _need(obj, key, where)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MoneyInputError(f"{where}{key} must be a whole number of at least {minimum}.")
    return value


def _bool(obj: Any, key: str, where: str) -> int:
    value = _need(obj, key, where)
    if not isinstance(value, bool):
        raise MoneyInputError(f"{where}{key} must be true or false.")
    return 1 if value else 0


def _list(obj: Any, key: str) -> list:
    value = obj.get(key) if isinstance(obj, dict) else None
    if not isinstance(value, list):
        raise MoneyInputError(f"The snapshot has no {key} list.")
    return value


def _utc(text: str, what: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MoneyInputError(f"{what} is not a date and time.") from exc
    if moment.tzinfo is None:
        raise MoneyInputError(f"{what} must say its time zone.")
    return moment.astimezone(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TS_FORMAT)


def _unique(keys: list, what: str) -> None:
    if len(set(keys)) != len(keys):
        raise MoneyInputError(f"The snapshot lists the same {what} twice.")


def _parse(body: Any) -> dict:
    """Check the whole snapshot and return the rows to write. Nothing is written here."""
    if not isinstance(body, dict):
        raise MoneyInputError("The snapshot must be a JSON object.")
    batch_id = _text(body, "batch_id", "")
    read_at = _stamp(_utc(_text(body, "read_at", ""), "read_at"))
    currency = _text(body, "currency", "")
    if currency != "EUR":
        raise MoneyInputError("Finary figures must arrive in euros.")

    ov = _need(body, "overview", "the ")
    overview = (
        _num(ov, "net_worth", "overview."), _num(ov, "gross_assets", "overview."),
        _num(ov, "liabilities", "overview."), _num(ov, "financial_assets", "overview."),
        _int(ov, "unvalued_liability_count", "overview."),
    )
    allocation = [(_text(a, "category", "allocation."), _num(a, "amount", "allocation.")) for a in _list(ov, "allocation")]
    _unique([a[0] for a in allocation], "allocation category")

    accounts = []
    for a in _list(body, "accounts"):
        accounts.append((
            _text(a, "account_id", "accounts."), _text(a, "name", "accounts."),
            _text(a, "account_type", "accounts."), _text(a, "currency", "accounts."),
            _num(a, "balance", "accounts."), _num(a, "value_eur", "accounts."),
            _opt_text(a, "institution", "accounts."), _opt_text(a, "sync_status", "accounts."),
            _opt_text(a, "last_successful_sync_at", "accounts."), _bool(a, "is_debt", "accounts."),
        ))
    _unique([a[0] for a in accounts], "account")

    holdings = []
    for n, h in enumerate(_list(body, "holdings")):
        holdings.append((
            _text(h, "account_id", "holdings."), n, _text(h, "name", "holdings."),
            _text(h, "holding_type", "holdings."), _opt_num(h, "quantity", "holdings."),
            _num(h, "current_value", "holdings."), _text(h, "currency", "holdings."),
            _opt_num(h, "buying_value", "holdings."),
        ))

    periods, categories = [], []
    for p in _list(body, "budget"):
        period = _text(p, "period", "budget.")
        if period not in ("this_month", "last_month"):
            raise MoneyInputError("budget.period must be this_month or last_month.")
        periods.append((
            period, _text(p, "from", "budget."), _text(p, "to", "budget."), _bool(p, "is_partial", "budget."),
            _int(p, "elapsed_days", "budget."), _int(p, "total_days", "budget.", 1),
            _num(p, "income", "budget."), _num(p, "expenses", "budget."), _num(p, "net_cashflow", "budget."),
            _opt_num(p, "savings_rate_percent", "budget."), _num(p, "recurring_monthly_equivalent", "budget."),
            _int(p, "recurring_count", "budget."),
        ))
        slugs = []
        for c in _list(p, "categories"):
            slugs.append(_text(c, "slug", "budget.categories."))
            categories.append((
                period, slugs[-1], _text(c, "label", "budget.categories."),
                _num(c, "expense", "budget.categories."), _num(c, "income", "budget.categories."),
                _opt_num(c, "monthly_average_expenses", "budget.categories."),
            ))
        _unique(slugs, "category")
    _unique([p[0] for p in periods], "budget period")

    months = [(_text(m, "month", "months."), _num(m, "income", "months."), _num(m, "expenses", "months."),
               _num(m, "net_cashflow", "months.")) for m in _list(body, "months")]
    _unique([m[0] for m in months], "month")
    for m in months:
        if not re.fullmatch(r"\d{4}-\d{2}", m[0]):
            raise MoneyInputError("months.month must look like 2026-10.")

    goals = []
    for g in _list(body, "goals"):
        goals.append((
            _text(g, "goal_id", "goals."), _text(g, "name", "goals."),
            _opt_num(g, "target_amount", "goals."), _opt_num(g, "current_amount", "goals."),
            _opt_text(g, "currency", "goals."), _opt_text(g, "status", "goals."),
            _opt_text(g, "target_date", "goals."),
        ))
    _unique([g[0] for g in goals], "goal")

    return {"batch_id": batch_id, "read_at": read_at, "currency": currency, "overview": overview,
            "allocation": allocation, "accounts": accounts, "holdings": holdings, "periods": periods,
            "categories": categories, "months": months, "goals": goals}


# ---------------------------------------------------------------- write

def ingest(conn: sqlite3.Connection, body: Any, received_at: Optional[datetime] = None) -> dict:
    """Store one snapshot whole, in one transaction. A batch_id seen before stores nothing."""
    rows = _parse(body)
    received = _stamp(received_at or datetime.now(timezone.utc))
    conn.execute("PRAGMA foreign_keys=ON")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM money_syncs WHERE batch_id = ?", (rows["batch_id"],)).fetchone():
            return {"duplicate": True}
        cur = conn.execute(
            "INSERT INTO money_syncs (batch_id, read_at, received_at, currency, net_worth, gross_assets,"
            " liabilities, financial_assets, unvalued_liability_count) VALUES (?,?,?,?,?,?,?,?,?)",
            (rows["batch_id"], rows["read_at"], received, rows["currency"], *rows["overview"]),
        )
        sid = cur.lastrowid
        many = [
            ("INSERT INTO money_allocation VALUES (?,?,?)", rows["allocation"]),
            ("INSERT INTO money_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows["accounts"]),
            ("INSERT INTO money_holdings VALUES (?,?,?,?,?,?,?,?,?)", rows["holdings"]),
            ("INSERT INTO money_budget_periods VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows["periods"]),
            ("INSERT INTO money_budget_categories VALUES (?,?,?,?,?,?,?)", rows["categories"]),
            ("INSERT INTO money_months VALUES (?,?,?,?,?)", rows["months"]),
            ("INSERT INTO money_goals VALUES (?,?,?,?,?,?,?,?)", rows["goals"]),
        ]
        for sql, items in many:
            conn.executemany(sql, [(sid, *item) for item in items])
    return {"duplicate": False, "sync_id": sid, "rows": {
        "accounts": len(rows["accounts"]), "holdings": len(rows["holdings"]),
        "budget_periods": len(rows["periods"]), "budget_categories": len(rows["categories"]),
        "months": len(rows["months"]), "goals": len(rows["goals"]), "allocation": len(rows["allocation"])}}


# ---------------------------------------------------------------- reads

def _r2(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)


def _newest(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM money_syncs ORDER BY read_at DESC, sync_id DESC LIMIT 1").fetchone()


def _is_stale(read_at: str, now: datetime) -> bool:
    return now - _utc(read_at, "read_at") > STALE_AFTER


def _savings(accounts: list[sqlite3.Row]) -> Optional[float]:
    rows = [a["value_eur"] for a in accounts if a["account_type"] == "saving" and not a["is_debt"]]
    return round(sum(rows), 2) if rows else None


def overview(conn: sqlite3.Connection, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sync = _newest(conn)
    if sync is None:
        return {"synced": False, "read_at": None, "stale": None, "net_worth": None, "gross_assets": None,
                "liabilities": None, "financial_assets": None, "unvalued_liability_count": None,
                "allocation": [], "accounts": [], "debts": [], "savings_eur": None, "series": [], "goals": []}
    sid, net = sync["sync_id"], sync["net_worth"]
    rows = conn.execute("SELECT * FROM money_accounts WHERE sync_id = ? ORDER BY value_eur DESC, name", (sid,)).fetchall()

    def shape(a: sqlite3.Row) -> dict:
        d = {k: a[k] for k in ("account_id", "name", "account_type", "currency", "balance", "value_eur",
                               "institution", "sync_status", "last_successful_sync_at")}
        d["is_debt"] = bool(a["is_debt"])
        d["balance"], d["value_eur"] = _r2(d["balance"]), _r2(d["value_eur"])
        d["share_of_net_worth"] = round(a["value_eur"] / net, 4) if net else None
        return d

    return {
        "synced": True, "read_at": sync["read_at"], "stale": _is_stale(sync["read_at"], now),
        "net_worth": _r2(net), "gross_assets": _r2(sync["gross_assets"]), "liabilities": _r2(sync["liabilities"]),
        "financial_assets": _r2(sync["financial_assets"]),
        "unvalued_liability_count": sync["unvalued_liability_count"],
        "allocation": [{"category": a["category"], "amount": _r2(a["amount"])} for a in conn.execute(
            "SELECT category, amount FROM money_allocation WHERE sync_id = ? ORDER BY amount DESC, category", (sid,))],
        "accounts": [shape(a) for a in rows if not a["is_debt"]],
        "debts": [shape(a) for a in sorted((a for a in rows if a["is_debt"]), key=lambda a: a["value_eur"])],
        "savings_eur": _savings(rows),
        "series": [{"read_at": s["read_at"], "net_worth": _r2(s["net_worth"])} for s in conn.execute(
            "SELECT read_at, net_worth FROM money_syncs ORDER BY read_at, sync_id")],
        "goals": [{"goal_id": g["goal_id"], "name": g["name"], "target_amount": _r2(g["target_amount"]),
                   "current_amount": _r2(g["current_amount"]), "currency": g["currency"], "status": g["status"],
                   "target_date": g["target_date"]} for g in conn.execute(
            "SELECT * FROM money_goals WHERE sync_id = ? ORDER BY name, goal_id", (sid,))],
    }


def _current_accounts(conn: sqlite3.Connection, sync_id: int) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM money_accounts WHERE sync_id = ? AND account_type = 'checking' AND is_debt = 0",
        (sync_id,)).fetchall()
    return {row["account_id"]: row for row in rows}


def _pair_flow(older: dict[str, sqlite3.Row], newer: dict[str, sqlite3.Row]) -> float:
    """Money out (positive) or in (negative) between two reads, over the accounts in both."""
    flow = 0.0
    for account_id, new in newer.items():
        old = older.get(account_id)
        if old is None:
            continue
        if new["balance"] != 0:
            rate = new["value_eur"] / new["balance"]
        elif old["balance"] != 0:
            rate = old["value_eur"] / old["balance"]
        else:
            rate = 0.0
        flow += (old["balance"] - new["balance"]) * rate
    return flow


def out_of_accounts(conn: sqlite3.Connection, day: date) -> dict:
    """How much left the current accounts on `day` and in its month so far. Writes nothing.

    Counted pair of reads by pair of reads on the checking accounts that are in both reads of the
    pair, so a transfer between two of them cancels and an exchange-rate change alone is not money out.
    A pair belongs to the Paris calendar day of its newer read.
    """
    conn.row_factory = sqlite3.Row
    reads = []
    for row in conn.execute("SELECT sync_id, read_at FROM money_syncs ORDER BY read_at, sync_id").fetchall():
        read_day = _utc(row["read_at"], "read_at").astimezone(PARIS).date()
        if read_day <= day:
            reads.append((row, read_day))
    accounts = [_current_accounts(conn, row["sync_id"]) for row, _ in reads]
    pairs = []
    for i in range(1, len(reads)):
        pairs.append({"older_day": reads[i - 1][1], "day": reads[i][1],
                      "flow": _pair_flow(accounts[i - 1], accounts[i])})

    todays = [p for p in pairs if p["day"] == day]
    today = None
    if todays:
        today = {"out": _r2(sum(p["flow"] for p in todays if p["flow"] > 0)),
                 "in": _r2(sum(-p["flow"] for p in todays if p["flow"] < 0))}

    first = day.replace(day=1)
    last_before = first - timedelta(days=1)
    mine = [p for p in pairs if p["day"] >= first and p["older_day"] >= last_before]
    month = None
    if mine:
        month = {"out": _r2(sum(p["flow"] for p in mine if p["flow"] > 0)),
                 "out_before": _r2(sum(p["flow"] for p in mine if p["flow"] > 0 and p["day"] < day)),
                 "since": None if mine[0]["older_day"] < first else mine[0]["older_day"].isoformat()}

    read_at = bank_as_of = None
    if reads:
        read_at = reads[-1][0]["read_at"]
        stamps = [a["last_successful_sync_at"] for a in accounts[-1].values()
                  if a["last_successful_sync_at"] is not None]
        bank_as_of = min(stamps) if stamps else None
    return {"day": day.isoformat(), "today": today, "month": month,
            "read_at": read_at, "bank_as_of": bank_as_of}


def category_view(spent: float, target: Optional[float], elapsed_days: int, total_days: int,
                  monthly_average_expenses: Optional[float]) -> dict:
    """One category against its target. pace = target x elapsed days / total days.

    ahead = spent / pace - 1 when pace > 0, else null. warn = more than 10% ahead of pace, or, for a
    target of 0, any spending at all. With no target there is no pace and no warning.
    """
    pace = None if target is None else target * elapsed_days / total_days
    ahead = spent / pace - 1 if pace else None
    if target is None:
        warn = False
    elif not pace:
        warn = spent > 0  # a zero target, or day 0 of the month: any spending is ahead of pace
    else:
        warn = ahead > WARN_AHEAD
    return {"spent": _r2(spent), "target": target, "pace": _r2(pace),
            "ahead": None if ahead is None else round(ahead, 3), "warn": warn,
            "suggestion": _r2(monthly_average_expenses)}


def budget(conn: sqlite3.Connection, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sync = _newest(conn)
    if sync is None:
        return {"synced": False, "read_at": None, "stale": None, "budget": None, "reason": None,
                "savings_eur": None, "cushion_months": None, "months": [], "last_month": None, "recurring": None}
    sid = sync["sync_id"]
    periods = {p["period"]: p for p in conn.execute("SELECT * FROM money_budget_periods WHERE sync_id = ?", (sid,))}
    targets = {t["slug"]: t["amount_eur"] for t in conn.execute("SELECT slug, amount_eur FROM budget_targets")}
    accounts = conn.execute("SELECT * FROM money_accounts WHERE sync_id = ?", (sid,)).fetchall()
    savings = _savings(accounts)
    this = periods.get("this_month")
    cutoff = this["from_date"][:7] if this else sync["read_at"][:7]
    months = [{"month": m["month"], "income": _r2(m["income"]), "expenses": _r2(m["expenses"]),
               "net_cashflow": _r2(m["net_cashflow"]), "complete": m["month"] < cutoff} for m in conn.execute(
        "SELECT * FROM money_months WHERE sync_id = ? ORDER BY month", (sid,))]
    done = [m for m in conn.execute("SELECT expenses FROM money_months WHERE sync_id = ? AND month < ?", (sid, cutoff))]
    mean = sum(m["expenses"] for m in done) / len(done) if done else 0
    cushion = round(savings / mean, 1) if savings is not None and mean > 0 else None

    def cats(period: str) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM money_budget_categories WHERE sync_id = ? AND period = ?"
                            " ORDER BY expense DESC, slug", (sid, period)).fetchall()

    result = {"synced": True, "read_at": sync["read_at"], "stale": _is_stale(sync["read_at"], now),
              "budget": None, "reason": None, "savings_eur": savings, "cushion_months": cushion,
              "months": months, "last_month": None, "recurring": None}
    if this is None:
        result["reason"] = "The newest Finary snapshot has no budget for this month."
    else:
        view = []
        for c in cats("this_month"):
            v = category_view(c["expense"], targets.get(c["slug"]), this["elapsed_days"], this["total_days"],
                              c["monthly_average_expenses"])
            view.append({"slug": c["slug"], "label": c["label"], "income": _r2(c["income"]), **v})
        result["budget"] = {
            "from": this["from_date"], "to": this["to_date"], "is_partial": bool(this["is_partial"]),
            "elapsed_days": this["elapsed_days"], "total_days": this["total_days"],
            "income": _r2(this["income"]), "expenses": _r2(this["expenses"]),
            "net_cashflow": _r2(this["net_cashflow"]), "savings_rate_percent": this["savings_rate_percent"],
            "categories": view,
        }
        result["recurring"] = {"monthly_equivalent": _r2(this["recurring_monthly_equivalent"]),
                               "count": this["recurring_count"]}
    last = periods.get("last_month")
    if last is not None:
        result["last_month"] = {
            "from": last["from_date"], "to": last["to_date"], "income": _r2(last["income"]),
            "expenses": _r2(last["expenses"]), "net_cashflow": _r2(last["net_cashflow"]),
            "categories": [{"slug": c["slug"], "label": c["label"], "spent": _r2(c["expense"]),
                            "income": _r2(c["income"])} for c in cats("last_month")],
        }
    return result


# ---------------------------------------------------------------- the dashboard's own targets

def _check_slug(slug: str) -> None:
    if not _SLUG.fullmatch(slug):
        raise MoneyInputError("A category name uses lowercase letters and underscores only.")


def list_targets(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [{"slug": t["slug"], "amount_eur": t["amount_eur"], "updated_at": t["updated_at"]}
            for t in conn.execute("SELECT * FROM budget_targets ORDER BY slug")]


def set_target(conn: sqlite3.Connection, slug: str, amount_eur: Any, now: Optional[datetime] = None) -> None:
    _check_slug(slug)
    if not _is_number(amount_eur) or amount_eur < 0:
        raise MoneyInputError("A target must be a number, zero or more.")
    with conn:
        conn.execute(
            "INSERT INTO budget_targets (slug, amount_eur, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(slug) DO UPDATE SET amount_eur = excluded.amount_eur, updated_at = excluded.updated_at",
            (slug, float(amount_eur), _stamp(now or datetime.now(timezone.utc))))


def delete_target(conn: sqlite3.Connection, slug: str) -> None:
    _check_slug(slug)
    with conn:
        if conn.execute("DELETE FROM budget_targets WHERE slug = ?", (slug,)).rowcount == 0:
            raise TargetNotFound("No target is set for that category.")
