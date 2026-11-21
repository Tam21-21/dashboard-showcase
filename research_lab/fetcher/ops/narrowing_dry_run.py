"""The pre-merge dry run for the one-minute contamination fix (brief S7 item 7, S3.3 item 4).

#229's own review promised its dry run a committed home; S8 gives the probe spec only synthetic
fixtures, which is NOT the same thing as this: the dry run's distinctive value is population-level
evidence over the REAL manifest and daily store, at zero quota cost, before a single row is
written under the new guard. It reads the manifest and the daily parquets ONLY -- it never issues
a network request, never calls set_coverage/mark_skipped/mark_done, and never writes anything.

For every 1h, 5min and 1min unit already in the manifest, this recomputes the span(s) that
interval's branch would request and applies the SAME narrowing function (tradinglife.
spans_overlapping_life) the real fetch path uses, then reports, per interval:
  - how many units the guard would now skip entirely (every span dropped);
  - of those, how many were dropped by the BOTTOM rule versus the TOP rule (S3.1's stated risk:
    an over-aggressive bottom trim is the unrecoverable failure mode, because it is a claim about
    where a ticker's one-minute history may begin that this unit cannot re-derive later);
  - for 1min specifically, the bottom-skip count is split by the unit's CURRENT status AND, for
    the 'done' ones, by whether the ticker carries a REUSE SIGNATURE.

The reuse split is the correction that makes this instrument honest, and it was added 2026-08-19
after the first run produced a false alarm. The first version reported 'bottom_done' alone and
called any non-zero value a STOP, reasoning that a 'done' unit holds real bars EODHD returned, so
bottom-skipping it proves the daily record's first bar is later than a real one-minute bar that
exists FOR THAT TICKER. The first half of that is true. The last four words are the part it could
not check, and they are where it was wrong: bars can also exist before a ticker's daily-recorded
first bar because the TICKER STRING WAS REUSED and those bars belong to the company that traded
under it earlier. That is the same contamination this whole unit exists to stop, arriving from the
bottom end instead of the top, and refusing it is the guard WORKING rather than over-reaching.

So the split is by reuse signature: a ticker is treated as reused when '<base>_old.US' also exists
in the manifest, which is EODHD's own record that the string changed hands. Measured on the real
store 2026-08-19, that split turned "117 units at risk" into 98 contaminated windows across two
tickers (AAN.US and ACHN.US, both proven by aggregating their one-minute bars into daily OHLC and
matching the _old twin's daily record byte-for-byte while their own daily record has no row for
those dates) and 19 genuinely-at-risk windows on one ticker (ADRU.US, an illiquid ETF with no
_old twin whose real sparse prints from 2004 predate EODHD's daily coverage of it).

EVERY NUMBER IN THE PARAGRAPH ABOVE IS A MEASUREMENT, NOT A FACT DERIVABLE FROM THIS TREE, so it is
cited rather than left for a reader to try to verify from the code and fail. The full method, the
price-match tables and the per-ticker detail are in `docs/log/bottom_trim_investigation_2026-08-19.md`;
the ruling that kept the bottom trim on this evidence is DECISIONS #263. The 1,617 reuse-candidate
figure quoted elsewhere in the review of this unit is from the same investigation and is reproduced
by `_reused_tickers` below over the real manifest. Re-running this script reproduces the split
itself; the price match does not live here, because comparing two daily records is not this
script's job.

The reuse signature is a SUFFICIENT reason to discount a bottom skip, never a necessary one: the
801-file five-minute audit measured 165 of 801 contaminated names as plain reused symbols with no
_old row at all, so a ticker WITHOUT the signature is not thereby proven clean. That asymmetry is
why only the no-signature half is reported as a STOP, and why the STOP text says "at least".

A non-zero bottom_done_no_reuse count is a STOP per S7 item 7, printed as such rather than left
for the reader to notice. A non-zero bottom_done_reused count is reported as INFORMATION, and it
is worth reading: those windows are contamination already sitting on disk, which is evidence for
DECISIONS #242's audit unit rather than a reason to hold this merge.

Usage (reads only; writes nothing):

  PYTHONPATH=. research_lab/worker/.venv/bin/python research_lab/fetcher/ops/narrowing_dry_run.py \\
      --staging-root /mnt/ssd/eodhd --manifest /mnt/ssd/eodhd/manifest.db

Defaults to the environment's EODHD_STAGING_ROOT / a manifest.db beside it if the flags are
omitted, so a real run needs no flags at all once EODHD_STAGING_ROOT is exported.
"""

import argparse  # --staging-root / --manifest flags
import os  # path join
import sqlite3  # READ-ONLY connection to the real manifest (uri mode=ro: cannot write even by accident)
import time  # 'now' for the 1h/5min span reconstruction

from research_lab.fetcher.config import RECENT_START, staging_root as default_staging_root
from research_lab.fetcher.tradinglife import (TradingLifeUnreadable, daily_date_span,
                                              daily_fetched_at, spans_overlapping_life,
                                              trading_life_from, _utc_date)
from research_lab.fetcher.windows import recent_1h_span, recent_5m_subwindows


def _life_for(conn, staging_root: str, ticker: str, cache: dict):
    """Read a ticker's trading life once per ticker, cached for the whole dry run.

    **It does NOT mirror `backfill._cached_life`, and the difference is deliberate on both sides.**
    That one caches only a SUCCESSFUL read, so a corrupt daily file fails the one unit that hit it
    and is retried under the existing retry ceiling rather than poisoning every remaining unit of
    that ticker. This one caches the `"unreadable"` sentinel too, because it is a MEASUREMENT: it
    wants exactly one read per ticker and it has no units to fail. An earlier version of this
    docstring said "mirrors _cached_life", which was false and would have misled anyone changing
    either.

    **Cost, MEASURED rather than argued.** The full run took 97.8 seconds wall clock on 2026-08-19
    against the laptop replica. It walks the 59,756 tickers carrying a 1h unit, then the same set
    for 5min (every one a cache hit), then the 1min unit rows; each ticker's daily file is read
    once, through this cache. A one-off pre-merge check, so it is not worth a thread pool. The
    production path offloads this same read with `asyncio.to_thread` because an inline 200-450 ms
    read on the USB SSD serialised a 16-worker pool (#229's second fault); that reasoning is about
    blocking an event loop and does not carry over here, and the review gate was right that the
    comment which used to sit at the read answered only that axis and never the total runtime.

    **The runtime is conditional on WHICH store it runs against**, which is the part worth carrying:
    97.8 s is the laptop replica on internal disk. Against the SSD over its measured USB 2.0 link
    the same walk is hours, not minutes. Point `--staging-root` at the replica when one exists,
    which is what the reproduce command in the module docstring does.

    **Three counts appear in this unit's documents and they are DIFFERENT POPULATIONS, not a
    disagreement** (a battery pass flagged them as one, which is why they are reconciled here):
    59,757 is the distinct tickers in `fetch_unit`; 59,756 is those carrying a 1h unit, which is
    what this walk costs; 59,747 is the daily units that are `done` AND carry a non-NULL
    `fetched_at`, the population the `FINALITY_MARGIN_DAYS` provenance entry measures because that
    entry needs a fetch stamp to compare against. Each number is right for its own question.
    """
    if ticker in cache:  # a hit
        return cache[ticker]  # the cached life tuple, None, or the "unreadable" sentinel
    try:
        # Sequential and on this thread, deliberately: see the docstring for the measured cost.
        span = daily_date_span(staging_root, ticker)
    except TradingLifeUnreadable:
        cache[ticker] = "unreadable"  # a distinct sentinel, never confused with None (no evidence)
        return "unreadable"
    life = trading_life_from(span, daily_fetched_at(conn, ticker))  # combine the span with the fetch stamp
    cache[ticker] = life  # cache the success (life may itself be None, meaning no daily evidence)
    return life  # the tuple, or None; the "unreadable" sentinel returns from the except branch above


def _classify_drop(span, life):
    """Return 'bottom', 'top' or None (not dropped) for one span against one life tuple."""
    if life is None:  # no daily evidence for this ticker
        return None  # no evidence -> never dropped
    first, last, last_is_final = life  # the recorded trading life
    frm, to = span  # the span's inclusive edges
    if _utc_date(to) < first:  # the bottom rule's own predicate, mirrored exactly
        return "bottom"
    if last_is_final and _utc_date(frm) > last:  # the top rule's own predicate, mirrored exactly
        return "top"
    return None


def _dry_run_1h_5min(conn, staging_root: str, life_cache: dict) -> dict:
    """Recompute the 1h and 5min spans for every ticker that has such a unit; classify skips."""
    now = int(time.time())  # one look-back reference for the whole recent-era reconstruction
    report = {"1h": {"total": 0, "would_skip": 0, "bottom": 0, "top": 0, "unreadable": 0},
             "5min": {"total": 0, "would_skip": 0, "bottom": 0, "top": 0, "unreadable": 0}}
    # The 1h lambda drops a None rather than passing [None] into spans_overlapping_life, which would
    # raise TypeError unpacking it. Same precondition backfill.py guards with a ValueError: it can
    # only bite if RECENT_START is not in the past. Guarded here too so the two stay symmetrical.
    for interval, span_fn in (("1h", lambda: [s for s in [recent_1h_span(RECENT_START, now)] if s]),
                              ("5min", lambda: recent_5m_subwindows(RECENT_START, now))):
        tickers = [r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM fetch_unit WHERE interval=?", (interval,))]
        for ticker in tickers:  # one ticker at a time; the life read is cached across both intervals
            life = _life_for(conn, staging_root, ticker, life_cache)  # cached; at most one read per ticker
            report[interval]["total"] += 1
            if life == "unreadable":
                report[interval]["unreadable"] += 1
                continue
            spans = span_fn()  # rebuild exactly the spans this interval's branch would request
            kept = spans_overlapping_life(spans, life)  # the SAME narrowing the real fetch path applies
            if kept:  # at least one span survives -> not a skip
                continue
            # Every span was dropped: classify by whichever rule fired for the FIRST span (the
            # branches request a single unit's worth of spans from one ticker's one life, so every
            # span drops for the same reason in practice; a mixed unit is vanishingly rare and would
            # still be counted once, conservatively, under whichever rule its first span shows).
            rule = _classify_drop(spans[0], life) if spans else None
            report[interval]["would_skip"] += 1
            if rule == "bottom":
                report[interval]["bottom"] += 1
            elif rule == "top":
                report[interval]["top"] += 1
    return report


def _reused_tickers(conn) -> set:
    """Every ticker whose string EODHD's own record shows changed hands.

    The signature is the presence of '<base>_old.US' alongside '<base>.US' in the manifest:
    EODHD retires a delisted name by suffixing it, so both rows existing means two different
    companies traded under the same string at different times. Read once per run, not per unit.
    """
    all_tickers = {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM fetch_unit")}
    # a plain ticker is 'reused' when its retired twin is also on record
    return {t for t in all_tickers if not t.endswith("_old.US")
            and t.replace(".US", "_old.US") in all_tickers}


def _dry_run_1min(conn, staging_root: str, life_cache: dict) -> dict:
    """Recompute every existing 1min manifest row's span; classify skips.

    The load-bearing number is `bottom_done_no_reuse`, NOT `bottom_done`. A 'done' unit is one
    EODHD actually returned real one-minute bars for, so bottom-skipping it proves bars exist
    before the daily record's first bar. What it does NOT prove is WHOSE bars those are. Where the
    ticker string was reused, they belong to the earlier occupant, and refusing them is this
    unit's own contamination guard working from the bottom end rather than a loss. So the 'done'
    bottom skips split two ways, and only the no-reuse half is a stop. See the module docstring
    for the measurement that forced this split.

    'empty' and 'error' units dropping is harmless either way (no real data existed to lose), so
    those stay counted separately and are never folded into the stop signal.
    """
    report = {"total": 0, "would_skip": 0, "bottom": 0, "top": 0, "unreadable": 0,
              "bottom_done": 0, "bottom_empty": 0, "bottom_error": 0,
              "bottom_done_reused": 0, "bottom_done_no_reuse": 0,
              "reused_examples": set(), "no_reuse_examples": set()}
    reused = _reused_tickers(conn)  # computed once; a per-unit query would be 1,760 round trips
    for ticker, ws, we, status in conn.execute(
        "SELECT ticker, window_start, window_end, status FROM fetch_unit WHERE interval='1min'"):
        life = _life_for(conn, staging_root, ticker, life_cache)  # cached; shared with the 1h/5min walk
        report["total"] += 1
        if life == "unreadable":
            report["unreadable"] += 1
            continue
        span = (ws, we - 1)  # exclusive tiling boundary becomes the inclusive API bound
        kept = spans_overlapping_life([span], life)  # one window, same narrowing as the real branch
        if kept:  # something survives, so this unit would still be requested
            continue
        rule = _classify_drop(span, life)  # which rule emptied it: bottom, top, or neither
        report["would_skip"] += 1
        if rule == "bottom":
            report["bottom"] += 1
            report[f"bottom_{status}"] = report.get(f"bottom_{status}", 0) + 1
            if status == "done":  # only a 'done' unit can represent real bars at risk
                if ticker in reused:
                    report["bottom_done_reused"] += 1
                    report["reused_examples"].add(ticker)
                else:
                    report["bottom_done_no_reuse"] += 1
                    report["no_reuse_examples"].add(ticker)
        elif rule == "top":
            report["top"] += 1
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-root", default=None, help="defaults to EODHD_STAGING_ROOT / config default")
    parser.add_argument("--manifest", default=None, help="defaults to <staging-root>/manifest.db")
    args = parser.parse_args()  # --staging-root / --manifest, both optional

    staging_root = args.staging_root or default_staging_root()  # flag wins, else the configured root
    manifest_path = args.manifest or os.path.join(staging_root, "manifest.db")
    print(f"narrowing dry run: staging_root={staging_root} manifest={manifest_path}")
    print("READ-ONLY: opens the manifest in mode=ro; writes nothing; issues no network request.\n")

    # mode=ro at the SQLite driver level: a bug in this script cannot write to the real manifest.
    conn = sqlite3.connect(f"file:{manifest_path}?mode=ro", uri=True)
    life_cache: dict = {}  # one cache for the whole dry run, shared by both walks
    try:
        recent_report = _dry_run_1h_5min(conn, staging_root, life_cache)
        deep_report = _dry_run_1min(conn, staging_root, life_cache)
    finally:
        conn.close()  # closed even if a walk raised

    print(f"{'interval':<8} {'total':>8} {'would_skip':>11} {'bottom':>8} {'top':>6} {'unreadable':>11}")
    for interval in ("1h", "5min"):
        r = recent_report[interval]  # this interval's counts
        print(f"{interval:<8} {r['total']:>8} {r['would_skip']:>11} {r['bottom']:>8} {r['top']:>6} {r['unreadable']:>11}")
    print(f"{'1min':<8} {deep_report['total']:>8} {deep_report['would_skip']:>11} "
         f"{deep_report['bottom']:>8} {deep_report['top']:>6} {deep_report['unreadable']:>11}")

    print(f"\n1min bottom-skip breakdown by the unit's CURRENT status:")
    print(f"  bottom_done={deep_report['bottom_done']}   <- real bars EODHD returned; split by reuse below")
    print(f"  bottom_empty={deep_report['bottom_empty']}  <- no data existed either way; harmless")
    print(f"  bottom_error={deep_report['bottom_error']}  <- never resolved either way; harmless")

    reused_n = deep_report["bottom_done_reused"]
    clean_n = deep_report["bottom_done_no_reuse"]
    print(f"\nthe 'done' bottom skips, split by REUSE SIGNATURE (the load-bearing split):")
    print(f"  bottom_done_reused={reused_n}  <- ticker has an '_old' twin on record, so the string "
         f"changed hands and these bars are the EARLIER occupant's. Refusing them is the "
         f"contamination guard working from the bottom end, NOT a loss. "
         f"tickers: {sorted(deep_report['reused_examples']) or '(none)'}")
    print(f"  bottom_done_no_reuse={clean_n}  <- no '_old' twin, so these are plausibly the ticker's "
         f"OWN bars predating EODHD's daily coverage of it. This is the real risk. "
         f"tickers: {sorted(deep_report['no_reuse_examples']) or '(none)'}")

    if clean_n > 0:
        # The known set as of the ruling. A ticker OUTSIDE it is a genuinely new finding and needs a
        # fresh decision; a ticker inside it was already weighed and ruled on, so printing an
        # undifferentiated STOP forever would make this script cry wolf at every future run.
        already_ruled = {"ADRU.US"}
        fresh = sorted(deep_report["no_reuse_examples"] - already_ruled)
        print(f"\nBOTTOM-TRIM EXPOSURE: {clean_n} already-fetched 1min unit(s), on "
             f"{len(deep_report['no_reuse_examples'])} ticker(s) with NO reuse signature, really did "
             f"return one-minute bars and would now be refused by the bottom trim. For those, S3.1's "
             f"daily-product-first-bar assumption is measurably FALSE. 'At least': the five-minute "
             f"audit measured 165 of 801 contaminated names as plain reused symbols carrying no "
             f"'_old' row, so absence of the signature does not prove a ticker is clean.")
        if fresh:
            print(f"\nSTOP: {fresh} is NOT in the set this exposure was ruled on. A new no-reuse "
                 f"ticker is a fresh instance of S3.1's assumption failing and needs its own "
                 f"decision before merging, per brief S7 item 7.")
        else:
            print(f"\nNo stop: every ticker above is in the set already ruled on. DECISIONS #263 "
                 f"(2026-08-19) weighed exactly this exposure and KEPT the bottom trim, because the "
                 f"loss is prospective only (these units are already 'done'), is recoverable by "
                 f"enumeration through the S3.3 item 5 reason template, and is far outweighed by the "
                 f"contamination the trim blocks. Evidence: "
                 f"docs/log/bottom_trim_investigation_2026-08-19.md.")
    else:
        print("\nNo already-fetched 1min unit on a non-reused ticker would be bottom-skipped: "
             "nothing to stop on.")
    if reused_n > 0:
        print(f"\nINFORMATION, not a stop: the {reused_n} reused-ticker window(s) above are "
             f"contamination ALREADY ON DISK. They are evidence for DECISIONS #242's audit unit, "
             f"which owns cleaning what is already written, and they are a reason to merge this "
             f"guard rather than to hold it.")


if __name__ == "__main__":
    main()
