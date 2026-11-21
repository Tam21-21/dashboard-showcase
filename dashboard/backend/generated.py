"""R-G25/AC-38: at dispatch, re-verify a strategy that carries a generation record against
that record, and refuse the dispatch on a mismatch. A strategy with NO generation record
reads as hand-authored and is let through unchanged (section 8.1's FRESH answer).

Standard library only: this module never imports vectorbtpro or anything from
research_lab. It reads two SQLite rows through the same researchdb connection helpers
every other read in this file uses, then re-derives one hash from plain text.
"""
from __future__ import annotations

import hashlib
import re

from dashboard.backend import researchdb

# Matches the one line research_lab/generator/emit.py writes: EXPRESSION = "<escaped text>".
# The capture group is the escaped literal INSIDE the quotes, unescaped below before hashing,
# so the hash is taken over the same raw expression text the generator originally hashed.
_EXPRESSION_LINE_RE = re.compile(r'^EXPRESSION = "((?:[^"\\]|\\.)*)"', re.MULTILINE)


class GeneratedSourceDrift(RuntimeError):
    """Raised when a generated strategy's current source no longer carries the expression
    its own generation record recorded. Carries both hashes so the 409 names what changed."""

    def __init__(self, strategy_id: int, recorded_sha256: str, current_sha256: "str | None"):
        self.strategy_id = strategy_id
        self.recorded_sha256 = recorded_sha256
        self.current_sha256 = current_sha256
        super().__init__(
            f"strategy {strategy_id} carries a generation record whose expression_sha256 "
            f"({recorded_sha256}) no longer matches the strategy's current source "
            f"(current expression_sha256={current_sha256!r}); dispatch refused (R-G25)"
        )


def _extract_expression(source_code: str) -> "str | None":
    """Read the EXPRESSION line back out of a strategy's source and unescape it to the raw
    text the generator originally hashed. Returns None if no such line exists (a strategy
    with no generation record never reaches this function anyway, see the FRESH branch
    below, but a defensive None is still the honest answer for a hand-edited file that
    deleted the line entirely)."""
    match = _EXPRESSION_LINE_RE.search(source_code)
    if match is None:
        return None
    escaped = match.group(1)
    # Reverse emit.py's own escaping: double-quote and backslash, in that order so a
    # literal backslash-quote sequence round-trips exactly (mirrors the emitter's own
    # replace order run in reverse).
    return escaped.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def assert_generation_record_matches(strategy_id: int) -> None:
    """R-G25: read the newest research_generated_candidates row for strategy_id; if none,
    return (let a hand-authored strategy through unchanged). Otherwise re-derive the
    sha256 of the expression embedded in the strategy's CURRENT source_code and compare it
    to the candidate row's own expression_sha256, raising GeneratedSourceDrift on a
    mismatch naming both hashes."""
    with researchdb.get_conn() as conn:
        candidate = conn.execute(
            "SELECT expression_sha256 FROM research_generated_candidates "
            "WHERE strategy_id = ? AND decision = 'accepted' "
            "ORDER BY generated_at DESC, id DESC LIMIT 1;",
            (strategy_id,),
        ).fetchone()
        if candidate is None:  # no generation record at all: hand-authored, let through (R-G25)
            return
        strategy_row = conn.execute(
            "SELECT source_code FROM research_strategies WHERE id = ?;",
            (strategy_id,),
        ).fetchone()

    recorded_sha256 = candidate["expression_sha256"]
    if strategy_row is None:  # the strategy itself is gone; nothing to compare against
        raise GeneratedSourceDrift(strategy_id, recorded_sha256, current_sha256=None)

    current_expression = _extract_expression(strategy_row["source_code"])
    current_sha256 = (
        hashlib.sha256(current_expression.encode("utf-8")).hexdigest()
        if current_expression is not None else None
    )
    if current_sha256 != recorded_sha256:
        raise GeneratedSourceDrift(strategy_id, recorded_sha256, current_sha256)


# The reserved prefix every generated strategy name carries (research_lab/generator/emit.py's
# own GENERATED_NAME_PREFIX). Duplicated as a literal here, deliberately, rather than imported:
# this module is standard-library-only by design (the docstring above), and importing
# research_lab.generator would pull a research_lab path onto dashboard's own import graph for
# a single string constant. AC-50's probe proves the two literals agree.
GENERATED_NAME_PREFIX = "gen__"


def query_generated_rows_without_candidate(conn) -> list[int]:
    """The monitor row `generated_rows_without_candidate` (AD13 failure A3, B2): every
    generated-namespace strategy id with no accepted generation record behind it. Restricted
    to the reserved prefix, or the query would return every hand-authored strategy too (R-G25
    and section 8.1's FRESH answer both call a hand-authored strategy legitimate). A query,
    not a counter: this function is a reader an ops-monitor tick calls, never a process-local
    tally (R-G19's rule: a declared failure mode's count is answerable by query)."""
    rows = conn.execute(
        "SELECT rs.id FROM research_strategies rs "
        "LEFT JOIN research_generated_candidates rgc "
        "  ON rgc.strategy_id = rs.id AND rgc.decision = 'accepted' "
        "WHERE rs.name LIKE ? ESCAPE '\\' AND rgc.id IS NULL;",
        (GENERATED_NAME_PREFIX.replace("_", "\\_") + "%",),
    ).fetchall()
    return [r["id"] for r in rows]
