"""research_lab/universe: the universe rule (lane UN, docs/plans/night/2026-09-10_set5a_night/
PLAN.md section 6, DECISIONS #329 ruling 3, #370). A PURE function evaluated per window, point in
time, never a fixed list of names: two calls for two different windows must never contaminate
each other (AD9), so this module holds no module-level state and no cached roster -- every
function re-reads its own inputs (the store, the reference CSV, the provenance register, the
tracker database) on every call, rather than remembering anything between calls.

Six filters, in the CORRECTED order the plan's own 2026-09-10 correction fixes (the minimum-count
gate reads the set surviving filters 1, 2, 3, 4 and 6 BEFORE filter 5's top-half cut, never after,
or Tamer's own 10-percent concentration argument would not reproduce):

  filter 1 (filter_common_stock)       -- Type == "Common Stock" in the committed reference CSV;
                                           a ticker absent from the reference is REFUSED, never
                                           assumed a common stock.
  filter 2 (filter_major_venue)        -- Exchange in research_lab.fetcher.universe.MAJOR_EXCHANGES
                                           (reused, never re-listed) AND bars covering the WHOLE
                                           window (a bar at or before window_start, a bar at or
                                           after window_end, both inclusive) AND at least
                                           derived.universe_lead_in_bars bars strictly BEFORE
                                           window_start (row `3.4c`, DECISIONS.md #406, ruled
                                           2026-09-20, Tamer's option (a)): a name qualifies for a
                                           screening universe only if the store also holds the
                                           sizing estimator's own lead-in before the window starts,
                                           so a universe never admits a name the stage would then
                                           refuse on stage_runner._check_warmups's own, unchanged,
                                           second guard. The pre-window count never feeds filter 4
                                           or 5's own liquidity measure below -- it exists only to
                                           decide membership, not to change how liquidity is scored.
                                           **Row `3.4d` (DECISIONS.md #428/#429, 2026-09-26): filter
                                           2 also excludes a name whose bars inside
                                           [window_start minus lead_in_bars, window_end] hold a
                                           close at or below zero** (CN's real chain cn22 failed on
                                           AEL's May-2024 non-positive close), read from the SAME
                                           one read_parquet per survivor this filter already pays
                                           for, never a second pass; the count removed is read
                                           through the companion reader bad_close_excluded_count,
                                           the same idiom lead_in_excluded_count already uses. A bad
                                           close OUTSIDE that span never excludes a name here --
                                           #429 is the correction that found the universe-side test
                                           alone insufficient, because the member FETCH (see
                                           dashboard/backend/researchdb.py's
                                           _resolve_or_create_member_dataset) still requested the
                                           canonical floor-to-now window regardless, which always
                                           covered a future bad close; that function now narrows the
                                           fetch's own end to the run's window_end when the raw
                                           store holds one beyond it, so the two fixes (exclude the
                                           name when the bad close is IN the span, narrow the fetch
                                           when it is OUTSIDE it) are both needed and neither alone
                                           was. **Lane D1 (2026-10-07): the same span test now
                                           excludes a name carrying ANY data check flag
                                           (research_lab.universe.data_check: adjusted price at
                                           EODHD's 999999.9999 ceiling, close at or below zero,
                                           high below low), still from the one read; counts per
                                           flag through data_check_excluded_count.**
  filter 3 (universe()'s own gate)     -- the #242 condemned-name list, read for its CONTENTS
                                           (2026-09-12 lane FL, closing read_1.md finding 1:
                                           presence is never permission). Every listed, held code
                                           is removed from the set surviving filters 1, 2 and 4,
                                           read through research_lab.fetcher.ops.quarantine_242's
                                           own load_condemned_csv convention (composed, never
                                           copied). Absence still raises
                                           ContaminationListUnavailable; a header-only or blank
                                           file raises ContaminationListEmpty. **2026-09-19 (`6.4c`,
                                           closing finding 2.1): a per-window empty intersection is
                                           NO LONGER a refusal.** Filter 2 already removes every one
                                           of the 801 condemned names from any window ending on or
                                           after 2020-01-01 (none of them has daily history reaching
                                           that date), so a CORRECT, COMPLETE list legitimately
                                           shares nothing with most windows, and the old
                                           window-based diagnosis would have refused every ordinary
                                           draw a correct list could ever produce. ContaminationListInert
                                           is raised instead when the loaded FILE itself does not
                                           match its own provenance record: its header claims to be
                                           a #242 copy (matches CONDEMNED_LIST_PROVENANCE_HEADER
                                           byte for byte) but its data-row count does not equal
                                           CONDEMNED_LIST_PROVENANCE_ROW_COUNT (DECISIONS.md #242,
                                           "The number is 801, exact") -- exactly what a truncated
                                           or otherwise corrupted copy of the committed file looks
                                           like. A file using a different header is not claiming to
                                           be a #242 copy at all and this check has nothing to say
                                           about it. The count filter 3 actually removes for a
                                           window is read through the companion reader
                                           filter3_excluded_count, never through universe()'s own
                                           return value (mirrors tickers_without_reference's own
                                           idiom), and is honestly 0 for a window whose survivors
                                           share nothing with the list -- a recorded fact now,
                                           never a refusal.
  filter 4 (liquidity_qualifying)      -- mean(adjusted_close * volume) over the window at or above
                                           search.universe_liquidity_floor, read from the register
                                           and NEVER written as a numeric literal in this file
                                           (docs/research_lab/parameter_provenance.md's own
                                           2026-09-10 amendment: the measure is
                                           adjusted_close * volume, never close * volume, because
                                           volume is restated to the current share basis across a
                                           name's whole history while close is the price as traded
                                           on the day, so close * volume inflates dollar volume by
                                           the cumulative split factor before a split).
  filter 6 (draw_held_out_names /
            held_out_names_for_calendar) -- HOLDOUT_FRACTION of the store-wide qualifying pool
                                           (common stock, major exchange, delisted names
                                           INCLUDED), a deterministic sample fixed by seed, drawn
                                           once per calendar through the real
                                           tracker.record_names_drawn and excluded from every
                                           window that shares that calendar.
  the minimum-count gate               -- >= search.universe_min_names_per_window (20 today) over
                                           the set surviving filters 1, 2, 3, 4 and 6, counted
                                           BEFORE filter 5. Below it the window is REFUSED, naming
                                           both the count seen and the count required.
  filter 5 (top_half_by_liquidity)     -- the top half of the gate-passing set, ranked by the same
                                           liquidity measure filter 4 uses. Applied AFTER the gate.

R-U6 (generator_requirements_2026-09-05.md section 7.3, widened 2026-09-10 to groups C and D):
universe() refuses to run, naming the key, before it ever touches the store or the #242 file, when
either search.universe_min_names_per_window or search.universe_liquidity_floor is OWED or absent
from the provenance register. No default is ever substituted for either.

Filter 2's lead-in test (row `3.4c`) reads a THIRD register key, derived.universe_lead_in_bars, a
group B value (never R-U6's own C/D scope: R-U6 as generator_requirements section 7.3 defines it
never widens to group B) -- but a value this rule cannot run without still deserves the identical
fail-loud treatment, not a silent default, so this module reuses the ONE refusal idiom it already
has (read_group_cd_value / UniverseParameterOwed) rather than inventing a second mechanism for a
value that happens to sit under a different tier letter. Read inside
qualifying_before_contamination, before the store's per-ticker files are opened, in the same place
the two R-U6 keys above are read; never a literal 278 in this module. Its value equals
sizing.estimator_window by construction (docs/research_lab/parameter_provenance.md's own entry),
since the two describe the SAME 278-bar span from two sides of the same guard: sizing.py's own
warmup and stage_runner.SIZING_LEAD_IN_BARS's AC-13 refusal.

DDIA (docs/books/kleppmann_canon.md, Chapter 4, line 859: "an index: an additional structure
derived from the primary data that speeds up reads at the cost of slower writes"; line 138:
"Primary-key index = one entry per row. Secondary index = lookup by other columns"). This build
adds no schema and no index: held_out_names_for_calendar's own read
(`WHERE event_kind = 'names_drawn' AND calendar_id = ?`) is answered by the partial UNIQUE index
`ux_draws_names_drawn_once` that dashboard/backend/researchdb.py's research_data_draws table
already carries (lane T, night two), built for exactly this access shape, so no new index is
warranted for a lookup the existing one already serves.
"""
from __future__ import annotations  # lets the string type hints below name this module's own classes before they are defined

import csv  # the committed symbol reference is a plain CSV; the standard library reads it without a schema
import random  # filter 6's own deterministic sample: random.Random(seed) never touches the process-wide RNG, so two draws never interfere
import re  # both the provenance-register text parser and the numeric-bounds extractor need pattern matching
from pathlib import Path  # every path this module takes may arrive as a str or a Path; normalising once keeps every helper simple

import pandas as pd  # the only way this module reads a parquet file's columns without loading vectorbtpro

from research_lab.fetcher.universe import MAJOR_EXCHANGES  # filter 2's venue list REUSED from the fetcher, never re-listed here (the brief's own instruction)
from research_lab.fetcher.ops.quarantine_242 import load_condemned_csv  # filter 3's own reader, COMPOSED (not copied): the same ticker-column-first, header-skipped-by-content convention quarantine_242 already established
from research_lab.universe import data_check  # lane D1: the three fact flags, defined once there; filter 2 drops a name with any of them in its span

# ---------------------------------------------------------------------------------------------
# Repo-relative defaults, computed from THIS FILE's own location rather than from the process's
# current working directory, so a caller in any directory resolves the same real files (AD9's
# purity requirement: a default must never depend on anything that could differ between two calls
# in the same process). These are static configuration constants, not a cached roster: nothing
# here is populated by a call and consulted by a later one, so two windows still cannot contaminate
# each other through them.
# ---------------------------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]  # research_lab/universe/__init__.py -> research_lab/universe -> research_lab -> the repo root
_DEFAULT_REFERENCE_PATH = _REPO_ROOT / "dashboard" / "backend" / "data" / "eodhd_symbol_reference.csv"  # the real, committed EODHD symbol reference (Act 1 of the 2026-09-10 plan)
_DEFAULT_PROVENANCE_PATH = _REPO_ROOT / "docs" / "research_lab" / "parameter_provenance.md"  # the real, committed parameter register R-U6 reads
DEFAULT_CONDEMNED_LIST_PATH = "dashboard/backend/data/eodhd_condemned_242.csv"  # a REPO-RELATIVE string (not a Path): the #242 list has no home in this repo today (PLAN.md section 13 item 2's named default (c)); this names where a committed one would live under option (a), and stays a plain string so any repo root can be joined onto it

# The two group C/D register keys this module reads, named as constants so a probe (and this
# module's own code) compares against ONE spelling rather than a string literal repeated in
# several places.
MIN_NAMES_KEY = "search.universe_min_names_per_window"  # the minimum-count gate's own key, group C as of the 2026-09-10 ruling
LIQUIDITY_FLOOR_KEY = "search.universe_liquidity_floor"  # filter 4's own key, group C, derived as the 5,000 slice over the 0.01 participation fraction (register Mechanism)

# Row `3.4c` (Tamer's ruling of 2026-09-20, option (a), DECISIONS.md #406): filter 2's own
# lead-in key. A GROUP B value, not group C/D like the two keys above, but read through the same
# refusal idiom (see the module docstring's own R-U6 paragraph). Its Bounds value is DERIVED, in
# bars, equal to sizing.estimator_window by construction, so a probe pins the two entries equal
# rather than letting either drift silently.
LEAD_IN_KEY = "derived.universe_lead_in_bars"

# Filter 6's own fraction: HOLDOUT_FRACTION of the store-wide pool is held out. POLICY, RULED
# directly in the requirements table (docs/briefs/generator_requirements_2026-09-05.md section 6
# item 6, "POLICY, RULED", cited to "Generator decision 3, and #329 ruling 3"), the same shape as
# filter 5's own top-half selection below: neither is a group A/B/C/D SWEEP knob in the register's
# own sense (#315), so neither gets a "## search.*" heading of its own; both are written directly
# into the ruled requirements document instead of the provenance register, which is why this
# constant is never read through read_group_cd_value the way the two keys above are.
HOLDOUT_FRACTION = 0.2  # 20 percent, PLAN.md section 6 item 6 / generator_requirements_2026-09-05.md section 6 item 6

_PARQUET_SUFFIX = ".US.parquet"  # every daily file in the store is named "<CODE>.US.parquet" (verified against the real store 2026-09-10)

# Filter 3's own FILE-shape provenance record (2026-09-19, `6.4c`, closing finding 2.1: a
# per-window empty intersection is not evidence of a truncated list -- filter 2 already removes
# every condemned name from any window ending on or after 2020-01-01, since none of them has daily
# history reaching that date, so a CORRECT, COMPLETE list legitimately excludes nothing in most
# windows and the old window-based diagnosis was always wrong). These two constants are the
# committed list's OWN provenance: DECISIONS.md #242, 2026-08-18, "The number is 801, exact," and
# the committed file's own first line, read byte for byte off
# dashboard/backend/data/eodhd_condemned_242.csv (verified 2026-09-19: `sed -n '1p'
# dashboard/backend/data/eodhd_condemned_242.csv`). A file whose own header matches this one is
# CLAIMING to be a copy of the #242 list, and its row count is checked against this figure; a file
# using a different header (an operator's own list, or this suite's own single-column 'ticker'
# fixture convention) is not claiming that at all and this module never checks it against these
# constants. A future LOGGED amendment to the committed list (finding 2.2's own price-test
# extension, still owed) updates both constants in the same commit that re-commits the file.
CONDEMNED_LIST_PROVENANCE_ROW_COUNT = 801  # DECISIONS.md #242, 2026-08-18: "The number is 801, exact."
CONDEMNED_LIST_PROVENANCE_HEADER = "ticker,daily_history_ends,five_min_file_bytes,copied_to_laptop"  # the committed file's own first line; Python's universal-newline text mode strips the file's own CRLF endings before this string is ever compared


class UniverseRuleError(RuntimeError):
    """Shared base for every refusal this module raises, so a caller that wants to catch 'any
    universe-rule refusal' has one name for it. Every probe in this suite still catches the
    specific leaf class it means, per the project's specific-exceptions-only rule."""


class ContaminationListUnavailable(UniverseRuleError):
    """Filter 3: the #242 condemned-name list is not a file this repo holds (today, always,
    under the default path). Carries the path that was checked and the count of names that
    survived filters 1, 2 and 4 and so WOULD have gone on to filter 3 had it been able to run."""

    def __init__(self, path: str, would_have_qualified_count: int) -> None:
        self.path = path  # the exact path this call checked, named so a caller sees precisely what was missing
        self.would_have_qualified_count = would_have_qualified_count  # the pre-contamination-check count (AC-U1's own payload)
        super().__init__(
            f"filter 3 cannot run: the #242 condemned-name list is not a file this repo holds "
            f"(looked for {path!r}); {would_have_qualified_count} names survive filters 1, 2 and 4 "
            f"and would have gone on to the contamination check"
        )  # a plain-English message carrying both facts, so a log line alone is debuggable


class ContaminationListEmpty(UniverseRuleError):
    """Filter 3: the #242 condemned-name list exists but names ZERO tickers (a header-only file,
    or a genuinely blank one). Read_1.md's own finding 1 (presence is never permission) closed:
    a truncated or misformatted copy that still passes the plain existence check must still be
    caught, and an empty membership set is the plainest sign of exactly that. Carries the path
    checked and the pre-contamination count that would have gone on to filter 3, the same payload
    shape as ContaminationListUnavailable, so every filter-3 refusal reads the same way."""

    def __init__(self, path: str, would_have_qualified_count: int) -> None:
        self.path = path  # the exact path this call checked, named so a caller sees precisely which file was empty
        self.would_have_qualified_count = would_have_qualified_count  # the pre-contamination-check count, same convention as ContaminationListUnavailable
        super().__init__(
            f"filter 3 refuses: the #242 condemned-name list at {path!r} names ZERO tickers "
            f"(a header-only or blank file); {would_have_qualified_count} names survive filters "
            f"1, 2 and 4 and would have gone on to the contamination check"
        )  # a plain-English message carrying both facts, so a log line alone is debuggable


class ContaminationListInert(UniverseRuleError):
    """Filter 3: the #242 condemned-name list's own FILE does not match its own provenance record.

    RAISED (2026-09-19, `6.4c`, closing finding 2.1) ONLY when the loaded file's own header line
    matches the committed list's own header byte for byte (CONDEMNED_LIST_PROVENANCE_HEADER,
    meaning the file is CLAIMING to be a copy of the #242 artifact) AND its data-row count does
    not equal the committed list's own provenance figure (CONDEMNED_LIST_PROVENANCE_ROW_COUNT,
    from DECISIONS.md #242: "The number is 801, exact") -- exactly what a truncated or otherwise
    corrupted copy of that SAME file looks like.

    NEVER raised any more on a per-window empty intersection with a real, complete list: finding
    2.1 measured that filter 2 (this module's own filter_major_venue) already removes every one of
    the 801 condemned names from any window ending on or after 2020-01-01, because none of them
    has daily history reaching that date, so a CORRECT, COMPLETE list legitimately shares nothing
    with most windows and the OLD window-based diagnosis (candidates.isdisjoint(condemned)) was
    always wrong -- it would have refused every ordinary draw a correct list could ever produce.
    A window whose own qualifying pool shares nothing with the list now passes silently where this
    class used to refuse; the count filter 3 removes is still visible through the existing,
    unchanged filter3_excluded_count reader, honestly 0 in that case rather than an exception.
    Never raised for a file using a DIFFERENT header (an operator's own list, or this suite's own
    single-column 'ticker' fixture convention): such a file is not claiming to be a #242 copy at
    all, so this class has nothing to say about it."""

    def __init__(self, path: str, would_have_qualified_count: int, actual_row_count: int) -> None:
        self.path = path  # the exact path this call checked, named so a caller sees precisely which file failed its own shape check
        self.would_have_qualified_count = would_have_qualified_count  # how many names survived filters 1, 2 and 4 in this window, carried for the same reason every filter-3 refusal carries it
        self.actual_row_count = actual_row_count  # the file's own data-row count as found, so the message is self-contained about WHAT was wrong, not just THAT something was wrong
        super().__init__(
            f"filter 3 refuses: the #242 condemned-name list at {path!r} carries this list's own "
            f"committed header but {actual_row_count} data rows, not the "
            f"{CONDEMNED_LIST_PROVENANCE_ROW_COUNT} DECISIONS.md #242 recorded for it -- a "
            f"truncated or otherwise corrupted copy of the committed list"
        )  # a plain-English message carrying the path and both counts, so a log line alone is debuggable


class UniverseParameterOwed(UniverseRuleError):
    """R-U6: a group C or D value this rule reads is OWED or absent from the provenance
    register. Never substitutes a default; the caller must fix the register or wait."""

    def __init__(self, key: str) -> None:
        self.key = key  # the exact register key that could not be read, named for the caller
        super().__init__(
            f"R-U6 refuses to run: {key!r} is OWED or absent from the provenance register "
            f"({_DEFAULT_PROVENANCE_PATH}); no default is ever substituted for a group C/D value"
        )


class TooFewQualifyingNames(UniverseRuleError):
    """The minimum-count gate: fewer than search.universe_min_names_per_window names survive
    filters 1, 2, 3, 4 and 6 in this window, counted BEFORE filter 5's top-half cut. Carries both
    numbers so a zero-name refusal (EMPTY SET) reads differently from a merely-too-small one."""

    def __init__(self, qualifying_count: int, minimum_required: int) -> None:
        self.qualifying_count = qualifying_count  # how many names actually survived (0 is a real, distinguishable value, never "missing")
        self.minimum_required = minimum_required  # the register's own current floor, carried so the message is self-contained
        super().__init__(
            f"only {qualifying_count} names survive filters 1, 2, 3, 4 and 6 in this window, "
            f"below the minimum of {minimum_required} required to search it at all"
        )


class HoldoutNotDrawnYet(UniverseRuleError):
    """T-ZERO / never acted on: this calendar has no names_drawn row yet. Never read as an empty
    holdout set, which would silently let every name through a window whose holdout was never
    actually drawn."""

    def __init__(self, calendar_id: "str | None") -> None:
        self.calendar_id = calendar_id  # the calendar that was asked for, named so the caller knows which draw to run
        super().__init__(
            f"calendar {calendar_id!r} has no names_drawn row yet; run draw_held_out_names for "
            f"it once before screening any window under it"
        )


def _store_codes(store: "str | Path") -> "frozenset[str]":
    """Every ticker code with a daily parquet file in the store, from the filenames alone (no
    file is opened here). A store directory that does not exist yet yields an empty result rather
    than raising, which is the FRESH/FIRST-RUN lifecycle state (item 9): pathlib's own glob on a
    missing directory returns no matches rather than raising."""
    store = Path(store)  # accept either a str or a Path uniformly from here on
    return frozenset(p.name[: -len(_PARQUET_SUFFIX)] for p in store.glob(f"*{_PARQUET_SUFFIX}"))  # strip the fixed suffix to recover the bare code


_CONDEMNED_EXCHANGE_SUFFIX = ".US"  # research_lab.fetcher.ops.quarantine_242's own ticker vocabulary always carries this (its _twin_ticker helper relies on the same fixed marker)


def _bare_condemned_codes(condemned: "set[str] | frozenset[str]") -> "frozenset[str]":
    """Convert quarantine_242's own ticker vocabulary (always "<CODE>.US", e.g. "AAAP_old.US",
    verified against the real committed list: every one of its 801 entries carries this suffix)
    into THIS module's own bare-code space (the space _store_codes and the reference CSV's own
    Code column both use, e.g. "AAAP_old", no exchange suffix at all). Filter 3's own set
    operations run entirely in the bare-code space, the same space every other filter in this
    module already uses, so this conversion happens exactly once, here, rather than at each call
    site. A ticker that does NOT carry the suffix is passed through unchanged rather than raising:
    it simply will not match any store code either way, which is the correct, honest outcome for
    an unexpected shape rather than a crash over one operator-supplied condemned file."""
    return frozenset(
        t[: -len(_CONDEMNED_EXCHANGE_SUFFIX)] if t.endswith(_CONDEMNED_EXCHANGE_SUFFIX) else t
        for t in condemned
    )


def _condemned_list_row_count_when_242_shaped(path: str) -> "int | None":
    """Filter 3's own FILE-shape check (2026-09-19, `6.4c`, replacing the old, wrong window-based
    diagnosis): does the file at `path` look like a copy of the committed #242 list at all, and if
    so, how many data rows does it actually carry?

    Returns None when the file's own header line does not match CONDEMNED_LIST_PROVENANCE_HEADER
    byte for byte: a file using a different, deliberately simpler shape (an operator's own list, or
    this suite's own single-column 'ticker' fixture convention,
    research_lab/probes/universe_rule/_fixtures.py's write_condemned_csv) is not CLAIMING to be a
    copy of the #242 artifact, so this check has nothing to say about it and the caller never
    compares its row count against the provenance figure.

    Returns the file's own DATA row count (header and blank lines excluded) when the header DOES
    match, so the caller compares that count against CONDEMNED_LIST_PROVENANCE_ROW_COUNT itself --
    a truncated copy has FEWER rows, a corrupted-and-padded copy could have MORE, and either is a
    mismatch the caller turns into a named refusal."""
    with open(path, "r", encoding="utf-8") as fh:  # default text mode: universal-newline translation strips the committed file's own CRLF endings before any comparison below
        lines = [line.rstrip("\n") for line in fh if line.strip()]  # drop the trailing newline and any wholly-blank line, matching load_condemned_csv's own blank-line tolerance
    if not lines:  # nothing at all; ContaminationListEmpty already refuses this earlier in universe()'s own call order, so this branch is defensive only and never reached from there
        return None
    header_line, data_lines = lines[0], lines[1:]  # the first line is the header by this file's own convention; every line after it is a data row
    if header_line != CONDEMNED_LIST_PROVENANCE_HEADER:  # not claiming to be a #242 copy at all
        return None
    return len(data_lines)  # exactly CONDEMNED_LIST_PROVENANCE_ROW_COUNT if the file is unchanged since it was committed; a different number otherwise


def _reference_rows(reference_path: "str | Path") -> "dict[str, dict[str, str]]":
    """The committed symbol reference, code -> its own row (Type/Exchange/Delisted), read fresh
    on every call (no module-level cache: two calls must never contaminate each other, AD9)."""
    rows: "dict[str, dict[str, str]]" = {}  # built fresh here on every call, never reused across calls
    with open(reference_path, newline="") as fh:  # newline="" is csv's own documented requirement for correct quoting
        for row in csv.DictReader(fh):  # DictReader keys itself from the header row (Code,Type,Exchange,Delisted)
            rows[row["Code"]] = row  # last row for a code wins; the real file has no duplicate code (Act 1's own measured count)
    return rows  # a plain dict, so a missing code's .get(code, {}) reads as "no data" rather than raising


def tickers_without_reference(store: "str | Path", reference_path: "str | Path") -> "frozenset[str]":
    """The monitor row universe_tickers_without_reference: every store code with NO row at all in
    the reference file, so filter 1 will refuse it rather than assume a common stock."""
    store_codes = _store_codes(store)  # everything the store physically holds
    referenced = set(_reference_rows(reference_path).keys())  # everything the reference file names, whatever its Type
    return frozenset(store_codes - referenced)  # set difference: in the store, absent from the reference


def filter_common_stock(candidates: "set[str] | frozenset[str]", reference_path: "str | Path") -> "frozenset[str]":
    """Filter 1: keep a candidate only when the reference file names it Type == 'Common Stock'.
    A candidate with no reference row at all is EXCLUDED, never assumed, and this never raises on
    one: `.get(code, {})` reads as an absent row rather than a KeyError."""
    reference_rows = _reference_rows(reference_path)  # fresh read, no cache, per AD9
    return frozenset(
        code for code in candidates  # every candidate offered in
        if reference_rows.get(code, {}).get("Type") == "Common Stock"  # survives only with a REAL Common Stock row
    )


def _ticker_coverage(store: "str | Path", code: str, window_start: str,
                      window_end: "str | None" = None,
                      lead_in_bars: "int | None" = None) -> "tuple[str, str, int, bool | None, frozenset[str] | None] | None":
    """A ticker's own first bar, last bar, the COUNT of its bars strictly before window_start, and
    (row `3.4d`, DECISIONS.md #428/#429) whether a non-positive close falls inside
    [window_start minus lead_in_bars bars, window_end] -- all four from the ONE read_parquet call
    filter 2 already paid for (row `3.4c`'s own criterion 1, and now row `3.4d`'s own: a new
    criterion must never cost a second pass over the store). The fourth element is only computed
    when BOTH window_end and lead_in_bars are given (filter 2's own call, which already knows
    both); a caller asking only the original three facts (lead_in_excluded_count) gets back
    `None` for it, unevaluated, exactly as nothing before this fold ever read a close value. None
    for the WHOLE tuple when the file is absent or genuinely empty, so a caller excludes rather
    than crashes -- unchanged from this function's pre-3.4c shape, `_ticker_date_bounds`, which
    returned only the first two of these values from the identical read.

    Lane D1 (2026-10-07): a FIFTH element, the data check's flags found inside the same span
    (`research_lab.universe.data_check`: adjusted price at EODHD's 999999.9999 ceiling, close at or
    below zero, high below low), from the same one read, `None` exactly when the fourth is `None`.
    The fourth element stays as it was, so the bad-close monitor row keeps its meaning."""
    path = Path(store) / f"{code}{_PARQUET_SUFFIX}"  # the store's own naming convention, verified against the real store
    if not path.is_file():  # a candidate the caller named but the store does not actually hold
        return None  # excluded by the caller, never a crash (NULL/ABSENT lifecycle state)
    frame = pd.read_parquet(path, columns=list(data_check.DAILY_COLUMNS))  # date plus every column the data check reads, still ONE read (row 3.4d's own rule, kept by lane D1)
    if frame.empty:  # a file that exists but carries zero rows
        return None  # the same "no usable history" outcome as a missing file
    dates = frame["date"]
    bars_before_window = int((dates < window_start).sum())  # strictly earlier than the window, the same comparison stage_runner._check_warmups uses
    has_bad_close_in_span: "bool | None" = None  # unevaluated unless the caller gives both window_end and lead_in_bars
    flags_in_span: "frozenset[str] | None" = None  # lane D1: evaluated exactly when has_bad_close_in_span is
    if window_end is not None:
        before_dates = dates[dates < window_start].sort_values()  # ascending; a positional index from the end finds the lead-in span's own start date, same convention as researchdb's own member-side mirror of this test
        if lead_in_bars is not None and lead_in_bars > 0 and len(before_dates) >= lead_in_bars:
            span_start = before_dates.iloc[len(before_dates) - lead_in_bars]  # exactly lead_in_bars bars strictly before window_start
        elif len(before_dates) > 0:
            span_start = before_dates.iloc[0]  # fewer bars exist than the lead-in asks for (or none was given): the whole available pre-window history is the span, never a crash
        else:
            span_start = window_start  # no bars at all before the window: the span collapses to [window_start, window_end]
        in_span = (dates >= span_start) & (dates <= window_end)  # row 3.4d's own closed span, inclusive both ends, matching filter 2's own inclusive coverage boundary
        has_bad_close_in_span = bool((frame.loc[in_span, "close"] <= 0).any())
        flags_in_span = data_check.flags_in(frame.loc[in_span])  # lane D1: every fact flag inside the same span
    return dates.min(), dates.max(), bars_before_window, has_bad_close_in_span, flags_in_span  # ISO date strings compare lexicographically exactly like calendar dates, min/max never assume sort order


def filter_major_venue(candidates: "set[str] | frozenset[str]", reference_path: "str | Path",
                        window_start: str, window_end: str, store: "str | Path",
                        lead_in_bars: int) -> "frozenset[str]":
    """Filter 2: on a major venue (Exchange in MAJOR_EXCHANGES, reused from the fetcher, never
    re-listed) AND present for the WHOLE window (a bar at or before window_start, a bar at or
    after window_end -- both inclusive, this coverage test's own boundary, distinct from filter
    4's half-open bar-selection boundary below) AND at least `lead_in_bars` bars strictly BEFORE
    window_start (row `3.4c`, DECISIONS.md #406, ruled 2026-09-20: a name qualifies for a
    screening universe only if the store also holds the sizing estimator's own lead-in before the
    window starts). `lead_in_bars` always arrives already read from the register by the caller,
    never re-derived here, so this function stays testable against a fixture value without ever
    touching the register file, matching `liquidity_qualifying`'s own `floor_value` convention
    below."""
    reference_rows = _reference_rows(reference_path)  # fresh read, no cache
    on_major_exchange = [  # the cheap, in-memory half of the filter, run BEFORE any file is opened
        code for code in candidates
        if reference_rows.get(code, {}).get("Exchange") in MAJOR_EXCHANGES  # the fetcher's own venue set, imported, never re-listed
    ]
    covering = set()  # accumulates names whose own history spans the whole window AND clears the lead-in AND carries no bad close in that span
    for code in on_major_exchange:  # only the exchange survivors pay for a file read
        coverage = _ticker_coverage(store, code, window_start, window_end, lead_in_bars)  # this name's own first bar, last bar, pre-window bar count AND row 3.4d's own bad-close flag, ONE read
        if coverage is None:  # no usable history at all (absent file or an empty one)
            continue  # excluded, never a crash
        first_bar, last_bar, bars_before_window, has_bad_close_in_span, flags_in_span = coverage  # unpack (min, max, count, bad-close, flags)
        covers_window = first_bar <= window_start and last_bar >= window_end  # inclusive on both ends, per the coverage convention u03 fixes
        clears_lead_in = bars_before_window >= lead_in_bars  # row `3.4c`'s own test, strictly-before, matching stage_runner._check_warmups
        if covers_window and clears_lead_in and not has_bad_close_in_span and not flags_in_span:  # lane D1: any data check flag in the span excludes the name too  # row `3.4d` (DECISIONS #428/#429): a non-positive close anywhere in the window-plus-lead-in span excludes the name here, never outside that span
            covering.add(code)  # this name was tradeable for the whole window, already warmed up before it started, and carries no bad close in that span
    return frozenset(covering)


def _liquidity_measure(store: "str | Path", code: str, window_start: str, window_end: str) -> "float | None":
    """The mean of adjusted_close * volume over bars strictly inside [window_start, window_end)
    (half-open: a bar stamped exactly at window_end belongs to the LATER window, matching the
    draw ledger's own convention, generator_architecture_2026-09-05.md AD7.5). adjusted_close,
    never close: docs/research_lab/parameter_provenance.md's 2026-09-10 amendment measured that
    volume is restated to the current share basis across a name's whole history while close is
    the price as traded on the day, so close * volume inflates dollar volume by the cumulative
    split factor for every date before a split. None when no bar falls inside the window at all,
    so a caller excludes rather than treating an empty mean as a false zero."""
    path = Path(store) / f"{code}{_PARQUET_SUFFIX}"  # the same naming convention as the coverage check above
    if not path.is_file():  # defensive: a caller may pass a code the store does not actually hold
        return None
    frame = pd.read_parquet(path, columns=["date", "adjusted_close", "volume"])  # only the three columns this measure needs
    in_window = frame[(frame["date"] >= window_start) & (frame["date"] < window_end)]  # half-open: >= start, < end
    if in_window.empty:  # the window falls entirely outside this name's own bars
        return None  # never a false qualify (u05's own NOBARS case, and u10's no-look-ahead case)
    return float((in_window["adjusted_close"] * in_window["volume"]).mean())  # the window's own average daily traded money


def liquidity_qualifying(candidates: "set[str] | frozenset[str]", window_start: str, window_end: str,
                          store: "str | Path", floor_value: float) -> "frozenset[str]":
    """Filter 4: keep a candidate only when its own average daily traded money over the window is
    at least floor_value (inclusive: 'at least', u05's own ATFLOOR case). floor_value always
    arrives already read from the register by the caller, never re-derived here, so this function
    stays testable against a fixture floor without ever touching the register file."""
    result = set()  # accumulates names clearing the floor
    for code in candidates:
        measure = _liquidity_measure(store, code, window_start, window_end)  # this name's own window-average
        if measure is not None and measure >= floor_value:  # None (no bars) never qualifies; "at least" is inclusive
            result.add(code)
    return frozenset(result)


def top_half_by_liquidity(candidates: "set[str] | frozenset[str]", window_start: str, window_end: str,
                           store: "str | Path") -> "frozenset[str]":
    """Filter 5: the top half of `candidates` by the SAME liquidity measure filter 4 uses,
    applied AFTER the minimum-count gate (PLAN.md section 2's own arithmetic: n qualifying names
    each sized at 2/n of the book only reproduces when n is counted before this cut). Odd counts
    round DOWN (floor division): the brief states no rounding rule for an odd n, and floor is the
    stricter, more conservative reading of 'the top half'. Ties (equal measures) break on the
    ticker code, ascending, so the ranking is fully deterministic given the same store."""
    ranked = []  # (code, measure) pairs; measure defaults to negative infinity so an unmeasurable name sorts last rather than crashing
    for code in candidates:
        measure = _liquidity_measure(store, code, window_start, window_end)  # recomputed, never cached: no module-level state
        ranked.append((code, measure if measure is not None else float("-inf")))  # a None here would mean the caller passed a name filter 4 never should have kept
    ranked.sort(key=lambda pair: (-pair[1], pair[0]))  # highest measure first; ties break by code, ascending, for determinism
    keep = len(ranked) // 2  # floor division: the stricter reading of "top half" on an odd count
    return frozenset(code for code, _ in ranked[:keep])  # the top `keep` codes only


def _store_wide_qualifying_pool(store: "str | Path", reference_path: "str | Path") -> "frozenset[str]":
    """Filter 6's own sampling pool: every ticker in the store, common stock, on a major exchange
    -- delisted names INCLUDED (PLAN.md item 6's own instruction), and the liquidity floor is
    DELIBERATELY not applied here, because it is a per-window average and a store-wide draw has
    no single window to average over; narrowing the pool by it would need choosing some window for
    the pool alone, which nothing specifies. This is the interface's own documented freedom
    (PROBES.manifest's INTERFACE NOTE): the exact pool composition is this suite's own choice, and
    what the probes pin are only its measured properties (delisted eligible, seed-reproducible,
    HOLDOUT_FRACTION of whatever the pool turns out to be). Because the pool is a superset of any
    single window's own qualifying set, drawn uniformly and independent of any window, the
    EXPECTED per-window held-out share of a window's own qualifying set is HOLDOUT_FRACTION itself
    (0.2); lane FL's real-store measurement over the 2015 window is the value the build's own
    report carries. The minimum-count gate's own 2/n concentration argument is UNAFFECTED by this:
    the gate counts survivors AFTER the holdout is removed (`survivors = candidates - held_out` in
    `_finish_screen`, the exact set the gate counts), never the pool's own size."""
    candidates = _store_codes(store)  # everything the store physically holds, active or delisted alike
    common_stock = filter_common_stock(candidates, reference_path)  # filter 1, unconditional on any window
    reference_rows = _reference_rows(reference_path)  # a second fresh read; CSV parsing is cheap next to even one parquet read
    return frozenset(
        code for code in common_stock
        if reference_rows.get(code, {}).get("Exchange") in MAJOR_EXCHANGES  # filter 2's venue half only; no window exists to test coverage against
    )


def draw_held_out_names(calendar_id: str, store: "str | Path", reference_path: "str | Path",
                         seed: int, now: int) -> "frozenset[str]":
    """Filter 6: draw HOLDOUT_FRACTION of the store-wide pool, deterministically by seed, and
    record the draw durably through the real tracker.record_names_drawn (a store-wide event
    belonging to no candidate, R-U4/AD9). At most one draw per calendar is enforced by the
    database's own partial unique index (ux_draws_names_drawn_once), never by this function: a
    second call for a calendar that already has a row lets sqlite3.IntegrityError propagate rather
    than swallowing it, exactly as tracker.record_names_drawn's own docstring requires."""
    pool = sorted(_store_wide_qualifying_pool(store, reference_path))  # a SORTED list: random.sample needs an ordered sequence, and sorting first makes the draw independent of set-iteration order
    member_count = round(len(pool) * HOLDOUT_FRACTION)  # HOLDOUT_FRACTION of the pool, rounded (never a genuine .5 tie for this fraction on an integer pool size)
    drawn = frozenset(random.Random(seed).sample(pool, member_count))  # a PRIVATE, seeded generator: never touches the shared random module's own global state
    from dashboard.backend import tracker  # local import: pure-filter callers (u01-u05, u07, u10) never need the dashboard package at all
    tracker.record_names_drawn(calendar_id, seed, member_count, now)  # the durable, store-wide event; raises sqlite3.IntegrityError unchanged on a repeat calendar
    return drawn  # the caller's own copy of what was just recorded


def held_out_names_for_calendar(calendar_id: str, store: "str | Path",
                                 reference_path: "str | Path") -> "frozenset[str]":
    """Re-derive an already-drawn calendar's held-out set from its recorded seed and
    member_count (AD9: 'the held-out set is an input, not a product', so no roster is ever
    stored; only the seed and the count are, and the membership is recomputed here every time).
    Raises HoldoutNotDrawnYet, never an empty set, when this calendar has never been drawn
    (T-ZERO / never acted on, item 9)."""
    from dashboard.backend import researchdb  # local import, matching draw_held_out_names above
    with researchdb.get_conn() as conn:  # a single read; no transaction needed for a SELECT
        row = conn.execute(
            "SELECT seed, member_count FROM research_data_draws "
            " WHERE event_kind = 'names_drawn' AND calendar_id = ?;",  # served by the existing partial unique index ux_draws_names_drawn_once; no new index needed (see the module docstring's DDIA note)
            (calendar_id,),
        ).fetchone()
    if row is None:  # this calendar has never had its holdout drawn
        raise HoldoutNotDrawnYet(calendar_id)  # a named refusal, never a silently empty set
    pool = sorted(_store_wide_qualifying_pool(store, reference_path))  # the SAME deterministic ordering draw_held_out_names used
    return frozenset(random.Random(row["seed"]).sample(pool, row["member_count"]))  # the same seed over the same ordered pool reproduces the same sample


def _entry_text(key: str, provenance_text: str) -> str:
    """The full body of one '## key' register entry, from its own heading to the next '## '
    heading or the end of the file. Raises UniverseParameterOwed(key) when no entry with this
    exact key exists at all (R-U6's NULL/ABSENT case, heading-missing half)."""
    headings = list(re.finditer(r"^## (?P<key>\S+)\s*$", provenance_text, re.MULTILINE))  # every top-level entry heading in the register, in order
    for i, heading in enumerate(headings):
        if heading.group("key") != key:  # not this entry; keep scanning
            continue
        start = heading.end()  # the entry's own body starts right after its heading line
        end = headings[i + 1].start() if i + 1 < len(headings) else len(provenance_text)  # up to the NEXT entry heading, or end of file for the last entry
        return provenance_text[start:end]
    raise UniverseParameterOwed(key)  # the heading itself is missing: R-U6's "absent" case


def read_group_cd_value(key: str, provenance_text: str) -> str:
    """Read one group C/D parameter's raw Bounds body from the provenance register's TEXT (never
    a path: a caller holding only a path reads it once and passes the text down, so this stays a
    pure string transform with no I/O of its own). Refuses (UniverseParameterOwed) rather than
    substituting a default in every one of R-U6's three cases: the '## key' heading is missing
    entirely; it exists but its '### Bounds' subsection is missing or empty; or the Bounds body
    is the literal word OWED. Never substitutes a default in between."""
    entry_text = _entry_text(key, provenance_text)  # raises UniverseParameterOwed itself when the heading is missing
    subsections = list(re.finditer(r"^### (?P<name>.+?)\s*$", entry_text, re.MULTILINE))  # every subsection heading inside this one entry
    for i, subsection in enumerate(subsections):
        if subsection.group("name") != "Bounds":  # not the Bounds subsection; keep scanning
            continue
        start = subsection.end()  # the Bounds body starts right after its own heading line
        end = subsections[i + 1].start() if i + 1 < len(subsections) else len(entry_text)  # up to the next subsection heading, or the entry's own end
        bounds_text = entry_text[start:end].strip()  # drop the surrounding blank lines the markdown carries
        if not bounds_text or re.match(r"^OWED\b", bounds_text):  # an empty body, or the literal OWED marker (this register's own convention)
            raise UniverseParameterOwed(key)
        return bounds_text  # a real, present value, returned as-is (still markdown, e.g. "**20**, a count of names")
    raise UniverseParameterOwed(key)  # the entry exists but carries no Bounds subsection at all


_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")  # a comma-grouped number first, else a plain digit run, each with an optional decimal part


def parse_numeric_bounds(bounds_text: str) -> float:
    """Extract the first numeric token from a Bounds body's raw text (e.g. '**500,000**, in the
    slice's own unit, DERIVED...' -> 500000.0), stripping thousands-separating commas. The FIRST
    number in reading order is always this register's own headline figure, written bold before
    any worked derivation that follows it in the same sentence (e.g. '5000 / 0.01')."""
    match = _NUMBER_RE.search(bounds_text)  # scan left to right; the first hit is the headline value by this register's own convention
    if match is None:  # a Bounds body read_group_cd_value already accepted, but one that carries no number at all
        raise ValueError(f"no numeric value found in Bounds text: {bounds_text!r}")  # a malformed register entry, a different failure from R-U6's own refusal
    return float(match.group(0).replace(",", ""))  # drop thousands separators before converting to a float


def qualifying_before_contamination(window_start: str, window_end: str, store: "str | Path",
                                     reference_path: "str | Path", provenance_text: str) -> "frozenset[str]":
    """Filters 1, 2 and 4 composed, exactly the count universe() reports as
    would_have_qualified_count on every filter-3 refusal (absent, empty or inert alike), and the
    filters-1/2/4 population filter 3 itself removes listed codes from. Takes the register's raw
    TEXT rather than a path, so a caller that already holds the text (universe(), screen(), the
    real-artifact probe) never re-reads the file a second time. Called EXACTLY ONCE per universe()
    call (2026-09-12 lane FL, closing read_1.md finding 2: this used to run twice, once here and
    once again inside screen()'s own body, a doubled pass over the whole store on the one path
    that matters -- u15's own call-counting probe proves the fix)."""
    floor_value = parse_numeric_bounds(read_group_cd_value(LIQUIDITY_FLOOR_KEY, provenance_text))  # R-U6 fires here if this key is owed
    lead_in_bars = int(parse_numeric_bounds(read_group_cd_value(LEAD_IN_KEY, provenance_text)))  # row `3.4c`'s own key, refused by name before the store is touched, same idiom as the line above
    candidates = _store_codes(store)  # everything the store physically holds
    common_stock = filter_common_stock(candidates, reference_path)  # filter 1
    major_venue = filter_major_venue(common_stock, reference_path, window_start, window_end, store, lead_in_bars)  # filter 2, lead-in included
    return liquidity_qualifying(major_venue, window_start, window_end, store, floor_value)  # filter 4


def filter3_excluded_count(window_start: str, window_end: str, store: "str | Path",
                            reference_path: "str | Path | None" = None,
                            provenance_path: "str | Path | None" = None,
                            condemned_list_path: "str | None" = None) -> int:
    """The companion reader beside filter 3 (mirrors tickers_without_reference's own established
    idiom): how many of a window's filters-1/2/4 population filter 3 itself removes, exposed
    WITHOUT changing universe()'s own return type (PLAN.md section 7 item 3's own interface
    choice, read_1.md's own ruling 1: a separate reader keeps every existing caller of universe()
    -- u01, u04, this suite's own AC-U1/AC-U9, and the stage-runner lane's own AC-15 -- unchanged).
    This is a monitoring QUESTION ("how many did it remove"), never the rule itself: it never
    raises ContaminationListEmpty or ContaminationListInert on an empty or non-overlapping list,
    it simply reports the intersection's size, which is honestly 0 in either case."""
    reference_path = reference_path if reference_path is not None else _DEFAULT_REFERENCE_PATH  # the real, committed reference by default
    provenance_path = provenance_path if provenance_path is not None else _DEFAULT_PROVENANCE_PATH  # the real, committed register by default
    condemned_list_path = condemned_list_path if condemned_list_path is not None else DEFAULT_CONDEMNED_LIST_PATH  # the same repo-relative default universe() resolves
    provenance_text = Path(provenance_path).read_text()  # read once here, this function's own single call
    candidates = qualifying_before_contamination(window_start, window_end, store, reference_path, provenance_text)  # filters 1, 2, 4, this function's own single call
    resolved_condemned_path = str(_REPO_ROOT / condemned_list_path)  # the same join rule universe() uses: an already-absolute path is returned unchanged
    condemned = _bare_condemned_codes(load_condemned_csv(resolved_condemned_path))  # raises QuarantineReadError, unchanged, on an absent or unreadable file; converted into the bare-code space candidates already uses
    return len(candidates & condemned)  # exactly the count universe() itself would remove from this same population


def lead_in_excluded_count(window_start: str, window_end: str, store: "str | Path",
                            reference_path: "str | Path | None" = None,
                            provenance_path: "str | Path | None" = None) -> int:
    """The companion reader beside filter 2's lead-in test (row `3.4c`, mirrors
    filter3_excluded_count's own idiom immediately above): how many names that clear filter 2's
    ORIGINAL two-sided coverage test (major venue, a bar at or before window_start, a bar at or
    after window_end) are then removed BECAUSE the store holds fewer than lead_in_bars bars
    strictly before window_start. This is a monitoring QUESTION, never the rule itself: it never
    raises on a window the lead-in test empties out, it simply reports how many the lead-in test
    alone removes from filter 2's own coverage-only survivors. Registry row
    universe_lead_in_excluded_per_window (REPORT-IN) reads this."""
    reference_path = reference_path if reference_path is not None else _DEFAULT_REFERENCE_PATH  # the real, committed reference by default
    provenance_path = provenance_path if provenance_path is not None else _DEFAULT_PROVENANCE_PATH  # the real, committed register by default
    provenance_text = Path(provenance_path).read_text()  # read once here, this function's own single call
    lead_in_bars = int(parse_numeric_bounds(read_group_cd_value(LEAD_IN_KEY, provenance_text)))  # R-U6-shaped refusal fires here if this key is owed
    candidates = _store_codes(store)  # everything the store physically holds
    common_stock = filter_common_stock(candidates, reference_path)  # filter 1, unconditional on any window
    reference_rows = _reference_rows(reference_path)  # a second fresh read; CSV parsing is cheap next to even one parquet read, same convention as _store_wide_qualifying_pool
    on_major_exchange = [
        code for code in common_stock
        if reference_rows.get(code, {}).get("Exchange") in MAJOR_EXCHANGES  # the fetcher's own venue set, imported, never re-listed
    ]
    excluded = 0  # names that cover the window on the ORIGINAL test but fall short of the lead-in specifically
    for code in on_major_exchange:  # only the exchange survivors pay for a file read, same as filter_major_venue itself
        coverage = _ticker_coverage(store, code, window_start)  # this name's own first bar, last bar and pre-window bar count, ONE read; no window_end/lead_in_bars given here so the fourth (bad-close) element stays unevaluated
        if coverage is None:  # no usable history at all (absent file or an empty one): never counted as a lead-in exclusion, it never covered the window either
            continue
        first_bar, last_bar, bars_before_window, _unused_bad_close, _unused_flags = coverage  # this reader uses only the first three elements
        covers_window = first_bar <= window_start and last_bar >= window_end  # filter 2's ORIGINAL boundary test, ignoring the lead-in
        if covers_window and bars_before_window < lead_in_bars:  # covers the window but falls short of the lead-in specifically
            excluded += 1
    return excluded


def bad_close_excluded_count(window_start: str, window_end: str, store: "str | Path",
                              reference_path: "str | Path | None" = None,
                              provenance_path: "str | Path | None" = None) -> int:
    """Row `3.4d`'s own companion reader (DECISIONS.md #428/#429, mirrors `lead_in_excluded_count`'s
    own idiom immediately above it): how many names that clear filter 2's ORIGINAL two-sided
    coverage-and-lead-in test (major venue, full window coverage, enough lead-in bars) are then
    removed BECAUSE the store holds a non-positive close inside [window_start minus lead_in_bars,
    window_end] specifically. This is a monitoring QUESTION, never the rule itself: it never
    raises, it simply counts, honestly 0 for a window the bad-close test removes nothing from.
    Registry row universe_bad_close_excluded_per_window (REPORT-IN) reads this."""
    reference_path = reference_path if reference_path is not None else _DEFAULT_REFERENCE_PATH  # the real, committed reference by default
    provenance_path = provenance_path if provenance_path is not None else _DEFAULT_PROVENANCE_PATH  # the real, committed register by default
    provenance_text = Path(provenance_path).read_text()  # read once here, this function's own single call
    lead_in_bars = int(parse_numeric_bounds(read_group_cd_value(LEAD_IN_KEY, provenance_text)))  # R-U6-shaped refusal fires here if this key is owed
    candidates = _store_codes(store)  # everything the store physically holds
    common_stock = filter_common_stock(candidates, reference_path)  # filter 1, unconditional on any window
    reference_rows = _reference_rows(reference_path)  # a second fresh read; CSV parsing is cheap next to even one parquet read, same convention as lead_in_excluded_count
    on_major_exchange = [
        code for code in common_stock
        if reference_rows.get(code, {}).get("Exchange") in MAJOR_EXCHANGES  # the fetcher's own venue set, imported, never re-listed
    ]
    excluded = 0  # names that cover the window AND clear the lead-in but carry a bad close in the span specifically
    for code in on_major_exchange:  # only the exchange survivors pay for a file read, same as filter_major_venue itself
        coverage = _ticker_coverage(store, code, window_start, window_end, lead_in_bars)  # this name's own first bar, last bar, pre-window bar count AND the bad-close flag, ONE read
        if coverage is None:  # no usable history at all: never counted, it never covered the window either
            continue
        first_bar, last_bar, bars_before_window, has_bad_close_in_span, _unused_flags = coverage  # unpack (min, max, count, bad-close, flags)
        covers_window = first_bar <= window_start and last_bar >= window_end  # filter 2's ORIGINAL two-sided coverage test
        clears_lead_in = bars_before_window >= lead_in_bars  # filter 2's ORIGINAL lead-in test
        if covers_window and clears_lead_in and has_bad_close_in_span:  # clears the ORIGINAL test but the bad-close test specifically removes it
            excluded += 1
    return excluded


def data_check_excluded_names(window_start: str, window_end: str, store: "str | Path",
                              reference_path: "str | Path | None" = None,
                              provenance_path: "str | Path | None" = None) -> "dict[str, tuple[str, ...]]":
    """Lane D1: which names the data check removes from this window, and why. Every name that clears
    filter 2's ORIGINAL coverage-and-lead-in test but carries a data check flag inside
    [window_start minus lead_in_bars, window_end], mapped to its sorted flags. Names, never just a
    count, so a person can see which stock left the universe. Empty when nothing is flagged; never
    raises on a window."""
    reference_path = reference_path if reference_path is not None else _DEFAULT_REFERENCE_PATH
    provenance_path = provenance_path if provenance_path is not None else _DEFAULT_PROVENANCE_PATH
    provenance_text = Path(provenance_path).read_text()
    lead_in_bars = int(parse_numeric_bounds(read_group_cd_value(LEAD_IN_KEY, provenance_text)))
    common_stock = filter_common_stock(_store_codes(store), reference_path)
    reference_rows = _reference_rows(reference_path)
    on_major_exchange = [code for code in common_stock
                         if reference_rows.get(code, {}).get("Exchange") in MAJOR_EXCHANGES]
    excluded = {}
    for code in on_major_exchange:
        coverage = _ticker_coverage(store, code, window_start, window_end, lead_in_bars)
        if coverage is None:
            continue
        first_bar, last_bar, bars_before_window, _unused_bad_close, flags_in_span = coverage
        if first_bar <= window_start and last_bar >= window_end and bars_before_window >= lead_in_bars and flags_in_span:
            excluded[code] = tuple(sorted(flags_in_span))
    return excluded


def data_check_excluded_count(window_start: str, window_end: str, store: "str | Path",
                              reference_path: "str | Path | None" = None,
                              provenance_path: "str | Path | None" = None) -> "dict[str, int]":
    """Lane D1's companion reader (mirrors `bad_close_excluded_count` just above): per data check flag,
    how many of `data_check_excluded_names`'s names carry it. A name with two flags counts once under
    each, so the values can sum to more than the number of names removed. Honestly 0 per flag when
    nothing is flagged. Drafted monitor row: universe_data_check_excluded_per_window (docs/lanes/D1.md)."""
    counts = {flag: 0 for flag in data_check.FLAGS}
    for flags in data_check_excluded_names(window_start, window_end, store, reference_path,
                                           provenance_path).values():
        for flag in flags:
            counts[flag] += 1
    return counts


def _finish_screen(candidates: "frozenset[str]", window_start: str, window_end: str, store: "str | Path",
                    calendar_id: "str | None", reference_path: "str | Path", min_names: int) -> "frozenset[str]":
    """The shared tail both screen() and universe() delegate to: filter 6 (the calendar's already-
    drawn holdout), the minimum-count gate, then filter 5 (top-half by liquidity). Takes the
    ALREADY-COMPUTED filters-1/2/4 (and, from universe(), post-filter-3) population as `candidates`
    rather than recomputing it, so qualifying_before_contamination itself is never called from in
    here -- the single place either caller's own single call to it happens is at their own top,
    which is what closes read_1.md's own finding 2 (u15's own probe)."""
    held_out = held_out_names_for_calendar(calendar_id, store, reference_path)  # filter 6: this calendar's already-drawn holdout
    survivors = candidates - held_out  # the exact set the minimum-count gate counts
    qualifying_count = len(survivors)  # a plain int, carried on a refusal so EMPTY (0) reads apart from merely too small
    if qualifying_count < min_names:  # ">= min_names" passes (inclusive), matching PLAN.md section 2's own arithmetic
        raise TooFewQualifyingNames(qualifying_count, min_names)  # names both numbers so the refusal is self-contained
    return top_half_by_liquidity(survivors, window_start, window_end, store)  # filter 5, applied only AFTER the gate has passed


def screen(window_start: str, window_end: str, store: "str | Path", calendar_id: "str | None" = None,
           reference_path: "str | Path | None" = None, provenance_path: "str | Path | None" = None) -> "frozenset[str]":
    """Filters 1, 2, 4 and 6, the minimum-count gate, then filter 5 -- everything EXCEPT filter 3
    (the #242 list): universe() is what layers filter 3's own membership test on top of this. This
    is the function most of the suite's own probes drive directly, exercising the rule without
    needing a committed condemned list at all."""
    reference_path = reference_path if reference_path is not None else _DEFAULT_REFERENCE_PATH  # the real, committed reference by default
    provenance_path = provenance_path if provenance_path is not None else _DEFAULT_PROVENANCE_PATH  # the real, committed register by default
    provenance_text = Path(provenance_path).read_text()  # read once here; qualifying_before_contamination takes the text, not the path
    min_names = int(parse_numeric_bounds(read_group_cd_value(MIN_NAMES_KEY, provenance_text)))  # R-U6 fires here if this key is owed
    candidates = qualifying_before_contamination(window_start, window_end, store, reference_path, provenance_text)  # filters 1, 2, 4 -- this call's own single site for this function
    return _finish_screen(candidates, window_start, window_end, store, calendar_id, reference_path, min_names)  # filter 6, the gate, then filter 5


def universe(window_start: str, window_end: str, store: "str | Path", calendar_id: "str | None" = None,
             reference_path: "str | Path | None" = None, provenance_path: "str | Path | None" = None,
             condemned_list_path: "str | None" = None) -> "frozenset[str]":
    """The top-level universe rule (AD9, #329 ruling 3): a pure function of one window, never a
    stored roster. Checks R-U6 for BOTH group C/D keys it reads before touching the store or the
    #242 file at all, then computes filters 1, 2 and 4 in its OWN single call to
    qualifying_before_contamination, then filter 3's own membership test against the condemned
    list's CONTENTS (2026-09-12 lane FL, closing read_1.md finding 1): absent refuses
    ContaminationListUnavailable; empty (header-only) refuses ContaminationListEmpty; a file
    claiming to be a #242 copy (its header matches) whose row count does not match that list's own
    provenance record refuses ContaminationListInert (2026-09-19, `6.4c`, closing finding 2.1: a
    per-window empty intersection is no longer checked at all, since filter 2 already removes
    every condemned name from any window ending on or after 2020-01-01, and a correct, complete
    list legitimately shares nothing with most such windows); otherwise every listed, held code is
    removed and the rest of the rule (filter 6, the minimum-count gate, filter 5) runs over the
    survivors through the shared _finish_screen tail."""
    reference_path = reference_path if reference_path is not None else _DEFAULT_REFERENCE_PATH  # the real, committed reference by default
    provenance_path = provenance_path if provenance_path is not None else _DEFAULT_PROVENANCE_PATH  # the real, committed register by default
    condemned_list_path = condemned_list_path if condemned_list_path is not None else DEFAULT_CONDEMNED_LIST_PATH  # a repo-relative default, resolved below

    provenance_text = Path(provenance_path).read_text()  # read once; reused for both R-U6 checks and passed down to qualifying_before_contamination as text
    min_names = int(parse_numeric_bounds(read_group_cd_value(MIN_NAMES_KEY, provenance_text)))  # R-U6, checked BEFORE the store or the #242 file (u08's own "checks BOTH...before" test); min-names first so its own refusal names THIS key when both are owed at once; its VALUE is captured here for _finish_screen's own gate, so this key's text is read only once
    read_group_cd_value(LIQUIDITY_FLOOR_KEY, provenance_text)  # R-U6's second key, same rule, checked in the same place (its value is read again, harmlessly, inside qualifying_before_contamination below)

    candidates = qualifying_before_contamination(window_start, window_end, store, reference_path, provenance_text)  # filters 1, 2, 4 -- THIS call is universe()'s own single site (u15's own probe proves it: exactly once per universe() call)

    resolved_condemned_path = str(_REPO_ROOT / condemned_list_path)  # pathlib's own join rule: an ALREADY-absolute condemned_list_path is returned unchanged, a relative one is joined onto the repo root
    if not Path(resolved_condemned_path).is_file():  # ABSENT: the #242 condemned list does not exist as a file this repo holds
        raise ContaminationListUnavailable(resolved_condemned_path, len(candidates))  # names the path checked and the pre-contamination count (AC-U1's own payload)

    condemned_raw = load_condemned_csv(resolved_condemned_path)  # composed, never copied: quarantine_242's own ticker-column-first, header-skipped-by-content convention
    if len(condemned_raw) == 0:  # EMPTY: the file exists but names zero tickers (header only, or genuinely blank)
        raise ContaminationListEmpty(resolved_condemned_path, len(candidates))  # names the path and the same pre-contamination count
    condemned = _bare_condemned_codes(condemned_raw)  # converted into the bare-code space `candidates` already uses (quarantine_242's own tickers always carry a ".US" suffix this module's own codes never do)

    # INERT, redefined (2026-09-19, `6.4c`, closing finding 2.1): tests the FILE, never a window.
    # A per-window empty intersection with a real, complete list is the ORDINARY outcome for any
    # window ending on or after 2020-01-01 (no condemned name's daily history reaches that date),
    # so treating it as evidence of truncation was always wrong -- it would refuse every ordinary
    # draw a correct list could ever produce. `actual_row_count` is None when this file's own
    # header does not match the committed list's own header at all (not claiming to be a #242
    # copy), in which case there is nothing to check and universe() proceeds unconditionally.
    actual_row_count = _condemned_list_row_count_when_242_shaped(resolved_condemned_path)
    if actual_row_count is not None and actual_row_count != CONDEMNED_LIST_PROVENANCE_ROW_COUNT:  # claiming to be a #242 copy but the row count does not match its own provenance record: truncated or otherwise corrupted
        raise ContaminationListInert(resolved_condemned_path, len(candidates), actual_row_count)  # names the path, the pre-contamination count and the file's own actual row count

    after_filter3 = candidates - condemned  # every listed, held code is removed here -- the exact gap read_1.md's finding 1 named (presence used to be permission on its own; it is not any more)

    return _finish_screen(after_filter3, window_start, window_end, store, calendar_id, reference_path, min_names)  # filter 6, the gate, then filter 5, over the POST-filter-3 survivors
