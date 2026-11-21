"""Validation on land (brief §7, folded by job 11), pure over a list of row dicts.

Time-series products (daily/1min/1h/5min): required fields + monotonic non-decreasing
timestamps. A repeated timestamp whose row is otherwise IDENTICAL on every required field is
FOLDED (kept once, counted) rather than failing the whole unit, because the old raise-on-first-
repeat behaviour turned one harmless vendor resend into a permanently abandoned unit (job 11,
docs/handoffs/2026-09-04_fetcher/11_job_download_preconditions.md, measured cost 718 ticker-and-
grain pairs). A repeated timestamp whose row DISAGREES on a price or volume field is a different
defect (real data corruption, not a resend) and still raises, loudly, with a named reason so it is
never silently resolved. Bulk products (cross-sectional, one day, no time axis): required fields +
no duplicate `code` + row count; no fold concept applies (there is no repeat to distinguish from a
resend). A structural violation raises ValidationError (the unit goes 'error' + re-queue); an empty
payload returns (0, 0) and the caller decides empty-vs-error from coverage.
"""

from .config import TIMESERIES_INTERVALS, BULK_INTERVALS  # the two product families


class ValidationError(RuntimeError):
    """A landed payload failed a structural check (missing field / non-monotonic / duplicate).

    Carries enough context (interval + the specific failure) to debug without the data.
    """


# Reason codes asserted BY NAME in the probes (job 11 AC-2/AC-3), replacing the old free-text-only
# match ("M-3's free-text match is replaced by a code", section 7 of the night-two plan). Importing
# the constant rather than hard-coding the string in a probe means a future rename cannot silently
# break the assertion without also breaking the probe's own import.
REASON_DUPLICATE_DISAGREES = "duplicate_disagrees"  # a repeated timestamp whose price/volume rows differ
REASON_NON_MONOTONIC = "non_monotonic"  # a timestamp that steps backward

# Required fields per product (brief §7), matched to the smoke-findings shapes.
REQUIRED_FIELDS = {
    "daily": ("date", "open", "high", "low", "close", "adjusted_close", "volume"),  # EOD incl. total-return
    "1min": ("timestamp", "open", "high", "low", "close", "volume"),  # intraday is raw (no adjusted_close)
    "1h": ("timestamp", "open", "high", "low", "close", "volume"),  # native recent-era hourly
    "5min": ("timestamp", "open", "high", "low", "close", "volume"),  # native recent-era 5-minute
    "bulk_eod": ("code", "open", "high", "low", "close", "adjusted_close", "volume"),  # keyed by code, no date
    "bulk_splits": ("code", "date", "split"),  # whole-exchange splits for one day
    "bulk_dividends": ("code", "date", "dividend"),  # whole-exchange dividends for one day
}

# The within-file time axis field per time-series product (used for monotonic/duplicate checks).
TS_FIELD = {"daily": "date", "1min": "timestamp", "1h": "timestamp", "5min": "timestamp"}


def _check_required(interval: str, row: dict) -> None:
    """Raise if any required field for this product is missing from a row."""
    for field in REQUIRED_FIELDS[interval]:  # every required key must be present
        if field not in row:  # fail loud on the first missing field, with context
            raise ValidationError(
                f"{interval}: row missing required field {field!r}: keys={sorted(row.keys())}"
            )


def validate(interval: str, rows: list) -> tuple:
    """Validate a landed payload for `interval`. Return (kept_rows, fold_count).

    kept_rows is the row list the caller should WRITE to the parquet, already folded: a repeated
    timestamp whose rows agree contributes exactly one entry (AC-1's own words, "one row kept per
    timestamp"). fold_count is how many repeated-but-identical rows were dropped from `rows` to
    produce `kept_rows`, so the caller can record the correction on the unit rather than
    swallowing it ("the fold COUNTED on the unit's record"). This function returns the ROWS rather
    than a bare count precisely so the caller can never write a payload that disagrees with what
    was validated: writing the original `rows` (duplicate still inside) while recording a smaller
    row_count would leave the parquet file and the manifest's own count out of step, which is the
    exact class of mismatch audit.py's content check exists to catch. `len(kept_rows)` is the row
    count to record. Empty (0 rows) returns ([], 0) without raising: the caller marks 'empty' or
    'error' based on whether coverage said data should exist. A non-empty payload that violates
    structure (a missing field, a genuine disagreement, a backward step) raises ValidationError so
    the unit is re-queued, never silently written.
    """
    if interval not in REQUIRED_FIELDS:  # guard: an unknown interval is a programming error
        raise ValidationError(f"unknown interval {interval!r}")
    if not isinstance(rows, list):  # the API must return a JSON array for every product
        raise ValidationError(f"{interval}: expected a list payload, got {type(rows).__name__}")
    if not rows:  # empty payload, a real coverage gap OR an empty bulk day; caller decides
        return [], 0  # not an error here; nothing to fold either

    for row in rows:  # every row must carry the required fields
        if not isinstance(row, dict):  # each element must be an object
            raise ValidationError(f"{interval}: expected object rows, got {type(row).__name__}")
        _check_required(interval, row)  # field presence

    if interval in TIMESERIES_INTERVALS:  # time-series: monotonic, with a fold for an identical repeat
        ts_field = TS_FIELD[interval]  # the within-file time axis
        # The fields a fold/disagreement decision compares. For every time-series product every
        # REQUIRED field besides the timestamp itself is a price or volume field (open/high/low/
        # close/volume, plus adjusted_close for daily), so "identical on every field" (AC-1's fold
        # condition) and "any price or volume field differs" (AC-2's raise condition) are the same
        # comparison here; there is no third kind of required field to tell apart.
        value_fields = [f for f in REQUIRED_FIELDS[interval] if f != ts_field]
        kept = []  # rows kept after folding exact repeats, payload order preserved
        fold_count = 0  # how many repeats were dropped because they matched the row already kept
        prev_ts = None  # the timestamp of the last row APPENDED to `kept` (not the last row seen)
        prev_row = None  # that row, to compare a repeat's fields against
        for row in rows:  # walk in payload order (EODHD returns ascending)
            ts = row[ts_field]  # the row's time key (epoch int or ISO date string)
            if prev_ts is not None and ts == prev_ts:  # a repeat of the immediately preceding timestamp
                # Monotonic non-decreasing order (enforced below) means any repeat of a timestamp
                # is always contiguous with the row already kept for it: two occurrences of the
                # same ts cannot have a different ts sandwiched between them without that different
                # ts itself violating monotonicity first. So comparing against `prev_row` (the one
                # kept row for this ts) is complete, not merely a same-step approximation.
                if all(row[f] == prev_row[f] for f in value_fields):  # identical on every price/volume field
                    fold_count += 1  # count the correction so an audit can see it happened
                    continue  # drop this row; the first occurrence is already in `kept`
                raise ValidationError(  # a real disagreement: a different defect, never silently resolved
                    f"{interval}: {REASON_DUPLICATE_DISAGREES} at timestamp {ts!r}: "
                    f"repeated row disagrees on a price or volume field"
                )
            if prev_ts is not None and ts < prev_ts:  # a step backward breaks monotonicity
                raise ValidationError(f"{interval}: {REASON_NON_MONOTONIC} timestamp {ts!r} after {prev_ts!r}")
            kept.append(row)  # a genuinely new timestamp
            prev_ts = ts  # advance
            prev_row = row  # remember it for the next repeat comparison
        return kept, fold_count  # rows to write, and how many were folded away
    elif interval in BULK_INTERVALS:  # cross-sectional bulk: no time axis -> duplicate-code check, no fold
        seen_codes = set()  # tickers already seen this day
        for row in rows:  # walk the ~50k cross-section
            code = row["code"]  # the per-ticker key
            if code in seen_codes:  # a repeated code in one day's bulk is a duplicate
                raise ValidationError(f"{interval}: duplicate code {code!r} in one bulk day")
            seen_codes.add(code)  # remember it
    return rows, 0  # every row is written unchanged; bulk products have no fold concept
