"""Re-fetch the daily files the 2026-10-08 store scan flagged, compare, and replace the improved ones.

Tamer 2026-10-09: re-fetch the flagged daily files to learn how many EODHD has since corrected (ESSA.US
was; SCCC.US was not), and keep the store in line with the source. Three steps, each resumable:

  fetch    one /eod request per flagged file (the backfill's own params and validation), written to
           <out>/daily/<file>; a file already there is skipped. Refuses up front if the calls would
           pass the day's cap minus margin (the live /user count, trusted only for today's UTC date).
  compare  the fresh copy is first cut at the old file's own last date and written to <out>/trimmed/,
           so a replacement corrects history without extending it: the store stays one as-of snapshot,
           and a window near its end never sees some stocks run months past the rest (which would read as
           the others delisting). Then old (store) against trimmed new per file: rows, first and last date,
           flagged rows by the data check (research_lab.universe.data_check), and the decision. A file is replaced when the
           fresh copy is non-empty and holds no more flagged rows than the old one: the store mirrors
           EODHD, as a backfill rerun would, and never trades a cleaner file for a dirtier one. An empty
           fresh copy (EODHD no longer serves the ticker) keeps the old file.
  apply    for every 'replace' row: the old file is copied to <backup>/ once, then the trimmed fresh one is moved
           in atomically (temp then rename). Writes <out>/applied.csv. Run it again with --store pointing at
           the SSD's raw/daily to bring the master copy in line before any store_sync, which would
           otherwise copy the old SSD file back over the laptop's (it compares sizes).

Extend mode (Tamer 2026-11-06, lane PX1): the store is brought up to one common last day, with the old
files deleted rather than backed up:

  select   <out>/scan.csv lists every store file whose last date is on or after --since (a file that ended
           long ago is a delisted stock and is left alone). Fetch then reads that scan.
  compare --extend-to DAY   the fresh copy is cut at DAY (not at the old last date). A DAY after today (UTC) or
           before any scanned old file's last date is refused before anything is written. The old file is kept
           when the fresh copy has less history; new_after_flagged counts flagged rows after the old last date.
  apply --no-backup         replaces with no backup. apply needs exactly one of --backup and --no-backup.

  join --extend-to DAY      (lane PX2, PX3) for the kept files: our old history up to the last shared day with the
           same close, adjusted_close times the ratio there, then EODHD's rows after that day up to DAY; old rows
           after that day are dropped. compare.csv gets join_note, join_day and join_rows_dropped.

Run from ~/trading with the worker venv:
  research_lab/worker/.venv/bin/python -m research_lab.fetcher.ops.refetch_flagged_daily fetch --scan CSV --out DIR
"""
import argparse
import asyncio
import csv
import datetime as dt
import os
import shutil
import sys
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from research_lab.fetcher.client import RateLimiter, fetch_json, read_api_key, read_usage
from research_lab.fetcher.config import (DEFAULT_DAILY_CAP, DEFAULT_DAILY_MARGIN, DEFAULT_HTTP_TIMEOUT_S,
                                         EOD_URL)
from research_lab.fetcher.validate import validate
from research_lab.universe import data_check

SUFFIX = ".parquet"


def flagged_daily_files(scan_csv: Path) -> list:
    """Every daily file the scan flagged, in the scan's own order."""
    with open(scan_csv, newline="") as fh:
        return [row["file"] for row in csv.DictReader(fh) if row["grain"] == "daily"]


def _write(path: Path, rows: list) -> None:
    """Temp file then rename, so a crash never leaves a half file under the final name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    pq.write_table(pa.Table.from_pylist(rows), tmp)
    os.replace(tmp, path)


async def _fetch_all(files: list, out: Path, concurrency: int) -> list:
    key = read_api_key()
    limiter = RateLimiter()
    todo = [f for f in files if not (out / "daily" / f).exists()]
    async with httpx.AsyncClient() as http:
        used, limit, _extra, used_date = await read_usage(http, key, DEFAULT_HTTP_TIMEOUT_S)
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        used_today = used if used_date == today else 0  # EODHD's count lags a day until the first call (#181)
        ceiling = min(DEFAULT_DAILY_CAP, limit) - DEFAULT_DAILY_MARGIN
        print(f"quota: used {used_today} today ({used} on {used_date}), ceiling {ceiling}, this run {len(todo)}")
        if used_today + len(todo) > ceiling:
            raise SystemExit(f"refusing: {len(todo)} calls would pass the ceiling {ceiling} (used {used_today})")
        results = []
        queue = asyncio.Queue()
        for f in todo:
            queue.put_nowait(f)

        async def worker():
            while True:
                try:
                    f = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                symbol = f[: -len(SUFFIX)]
                params = {"api_token": key, "fmt": "json", "period": "d", "order": "a"}  # backfill's own params
                await limiter.acquire()
                try:
                    rows = await fetch_json(http, EOD_URL.format(symbol=symbol), params, f"{symbol} eod",
                                            DEFAULT_HTTP_TIMEOUT_S)
                    kept, folded = validate("daily", rows)
                    if kept:
                        _write(out / "daily" / f, kept)
                    results.append({"file": f, "status": "ok" if kept else "empty", "rows": len(kept),
                                    "folded": folded, "error": ""})
                except Exception as exc:  # noqa: BLE001 -- one file isolated and recorded (DECISIONS #432)
                    results.append({"file": f, "status": "error", "rows": 0, "folded": 0,
                                    "error": f"{type(exc).__name__}: {exc}"[:300]})
                if len(results) % 500 == 0:
                    print(f"  {len(results)}/{len(todo)}", flush=True)

        await asyncio.gather(*(worker() for _ in range(concurrency)))
    return results


def cmd_fetch(args) -> int:
    out = Path(args.out)
    files = flagged_daily_files(Path(args.scan))
    results = asyncio.run(_fetch_all(files, out, args.concurrency))
    log = out / "fetch_log.csv"
    new = not log.exists()
    with open(log, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "status", "rows", "folded", "error"])
        if new:
            w.writeheader()
        w.writerows(results)
    counts = pd.Series([r["status"] for r in results]).value_counts().to_dict()
    print(f"fetched {len(results)} of {len(files)} flagged daily files: {counts}")
    return 1 if counts.get("error") else 0


def _summary(path: Path) -> dict:
    if not path.exists():
        return {"rows": 0, "first": "", "last": "", "flagged": 0, "flags": ""}
    frame = pd.read_parquet(path)
    flagged = data_check.flag_rows(frame)
    return {"rows": len(frame), "first": str(frame["date"].iloc[0]) if len(frame) else "",
            "last": str(frame["date"].iloc[-1]) if len(frame) else "",
            "flagged": len(flagged), "flags": ";".join(f"{k}={v}" for k, v in flagged["flag"].value_counts().items())}


def _trim_to_old_end(fresh: Path, old: Path, dest: Path) -> None:
    """Write the fresh copy cut at the old file's last date (dates are ISO strings, so they sort)."""
    frame = pd.read_parquet(fresh)
    table = None
    if old.exists():
        old_last = str(pd.read_parquet(old, columns=["date"])["date"].iloc[-1])
        frame = frame[frame["date"].astype(str) <= old_last]
        # The old file's own schema, so a replaced file reads exactly like its neighbours (a cast that
        # would lose values, a fractional volume, raises rather than rounding).
        table = pa.Table.from_pandas(frame, preserve_index=False).cast(pq.read_schema(old))
    if table is None:
        table = pa.Table.from_pandas(frame, preserve_index=False)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".tmp.{os.getpid()}")
    pq.write_table(table, tmp)
    os.replace(tmp, dest)


def _cut_at(fresh: Path, old: Path, dest: Path, day: str) -> None:
    """Write the fresh copy cut at DAY (inclusive), in the old file's schema when there is an old file."""
    frame = pd.read_parquet(fresh)
    frame = frame[frame["date"].astype(str) <= day]
    table = pa.Table.from_pandas(frame, preserve_index=False)
    if old.exists():
        table = table.cast(pq.read_schema(old))  # a fractional volume raises rather than rounding
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".tmp.{os.getpid()}")
    pq.write_table(table, tmp)
    os.replace(tmp, dest)


def _last_date(path: Path) -> str:
    """The last date of a store file, or "" when the file is missing or has no rows."""
    if not path.exists():
        return ""
    dates = pd.read_parquet(path, columns=["date"])["date"]
    return str(dates.iloc[-1]) if len(dates) else ""


def _check_extend_day(day: str, store: Path, files: list) -> None:
    """Refuse a DAY the extend cannot honour, before any file is written."""
    try:
        parsed = dt.date.fromisoformat(day)
    except ValueError:
        raise SystemExit(f"--extend-to {day!r} is not a YYYY-MM-DD date")
    today = dt.datetime.now(dt.timezone.utc).date()
    if parsed > today:
        raise SystemExit(f"refusing: --extend-to {day} is after today (UTC {today})")
    for f in files:
        last = _last_date(store / f)
        if last and day < last:
            raise SystemExit(f"refusing: --extend-to {day} is before {f}'s last date {last}")


def _extend_decision(old_frame: pd.DataFrame, cut_frame: pd.DataFrame, schema_error: str, has_fresh: bool) -> tuple:
    """(decision, new_after_flagged) for one file in extend mode, in the order the lane plan fixes."""
    old_last = str(old_frame["date"].iloc[-1]) if len(old_frame) else ""
    before = cut_frame[cut_frame["date"].astype(str) <= old_last]
    flag_dates = data_check.flag_rows(cut_frame)["date"].astype(str)
    after_flagged = int((flag_dates > old_last).sum())
    before_flagged = int((flag_dates <= old_last).sum())
    if schema_error:
        return "keep_fresh_schema_differs", after_flagged
    if not has_fresh:
        return "keep_no_fresh_copy", after_flagged
    if len(old_frame) and len(before) == 0:
        return "keep_fresh_empty_before_old_end", after_flagged
    if len(old_frame) and (len(before) < len(old_frame) or str(before["date"].iloc[0]) > str(old_frame["date"].iloc[0])):
        return "keep_fresh_history_shorter", after_flagged
    if before_flagged > len(data_check.flag_rows(old_frame)):
        return "keep_fresh_is_worse", after_flagged
    if len(old_frame) and cut_frame.reset_index(drop=True).equals(old_frame.reset_index(drop=True)):
        return "keep_identical", after_flagged
    return "replace", after_flagged


def cmd_select(args) -> int:
    store, out = Path(args.store), Path(args.out)
    if not store.is_dir():
        raise SystemExit(f"store {store} is not a directory")
    try:
        dt.date.fromisoformat(args.since)
    except ValueError:
        raise SystemExit(f"--since {args.since!r} is not a YYYY-MM-DD date")
    paths = sorted(store.glob("*" + SUFFIX))
    lasts = {p.name: _last_date(p) for p in paths}
    chosen = [name for name, last in lasts.items() if last != "" and last >= args.since]
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "scan.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["grain", "file"])
        for name in chosen:
            w.writerow(["daily", name])
    print(f"selected {len(chosen)} of {len(paths)} files")
    return 0


def cmd_compare(args) -> int:
    store, out = Path(args.store), Path(args.out)
    files = flagged_daily_files(Path(args.scan))
    extend_to = args.extend_to
    if extend_to is not None:
        _check_extend_day(extend_to, store, files)
    rows = []
    for f in files:
        schema_error = ""
        if (out / "daily" / f).exists():
            try:
                if extend_to is not None:
                    _cut_at(out / "daily" / f, store / f, out / "trimmed" / f, extend_to)
                else:
                    _trim_to_old_end(out / "daily" / f, store / f, out / "trimmed" / f)
            except pa.ArrowInvalid as exc:  # e.g. a fractional volume in a file stored with int64 volume
                schema_error = str(exc)[:200]
                (out / "trimmed" / f).unlink(missing_ok=True)
        old, new = _summary(store / f), _summary(out / "trimmed" / f)
        if extend_to is not None:
            has_fresh = (out / "daily" / f).exists()
            old_frame = pd.read_parquet(store / f) if (store / f).exists() else pd.DataFrame(columns=["date"])
            cut_frame = pd.read_parquet(out / "trimmed" / f) if (out / "trimmed" / f).exists() else old_frame.iloc[0:0]
            decision, after_flagged = _extend_decision(old_frame, cut_frame, schema_error, has_fresh)
            clean = (out / "trimmed" / f).exists() and new["flagged"] == 0
            rows.append({"file": f, **{f"old_{k}": v for k, v in old.items()}, **{f"new_{k}": v for k, v in new.items()},
                         "new_after_flagged": after_flagged, "fresh_clean": clean, "decision": decision,
                         "note": schema_error})
            continue
        if schema_error:
            decision = "keep_fresh_schema_differs"
        elif not (out / "daily" / f).exists():
            decision = "keep_no_fresh_copy"
        elif new["flagged"] > old["flagged"]:
            decision = "keep_fresh_is_worse"
        else:
            decision = "replace"
        if decision == "replace" and (store / f).exists() and pd.read_parquet(store / f).equals(pd.read_parquet(out / "trimmed" / f)):
            decision = "keep_identical"  # nothing to correct: EODHD still serves the same history
        if not schema_error and (out / "daily" / f).exists() and new["rows"] == 0:
            decision = "keep_fresh_empty_before_old_end"
        clean = (out / "trimmed" / f).exists() and new["flagged"] == 0
        rows.append({"file": f, **{f"old_{k}": v for k, v in old.items()}, **{f"new_{k}": v for k, v in new.items()},
                     "fresh_clean": clean, "decision": decision, "note": schema_error})
    frame = pd.DataFrame(rows)
    frame.to_csv(out / "compare.csv", index=False)
    print(f"compared {len(frame)} files")
    print("decisions:", frame["decision"].value_counts().to_dict())
    print("fresh copy clean (no flagged rows):", int(frame["fresh_clean"].sum()))
    print(f"flagged rows: old {int(frame['old_flagged'].sum())}, new {int(frame['new_flagged'].sum())}")
    return 0


JOINABLE = ("keep_fresh_history_shorter", "keep_fresh_is_worse", "keep_fresh_empty_before_old_end")
ADJ_CEILING = 999999.9999  # the store's flag value for an adjusted_close that could not be computed


def _positive(value) -> bool:
    return bool(pd.notna(value) and value > 0)


def _join_one(old_path: Path, fresh_path: Path, dest: Path, day: str) -> tuple:
    """Join one file; return (join_note, join_day, rows_dropped). `dest` is written only when the note is "".

    The anchor A is the last shared day with the same close: the newest date on or before J (the old last
    day) present in both copies where the old and fresh close agree (rtol 1e-6) and both adjusted_close are
    above 0. The old rows dated on or before A stay, adjusted_close times r (the fresh over the old
    adjusted_close on A); the old rows after A are dropped; then come the raw fresh rows dated after A up to
    `day`. Notes: old_empty, no_common_day, no_same_close_day, scaled_past_ceiling, schema_differs.
    """
    old = pd.read_parquet(old_path)
    if len(old) == 0:
        return "old_empty", "", ""
    fresh = pd.read_parquet(fresh_path)
    j = str(old["date"].iloc[-1])
    old_by_day = old.assign(date=old["date"].astype(str)).drop_duplicates("date", keep="last").set_index("date")
    fresh_by_day = fresh.assign(date=fresh["date"].astype(str)).drop_duplicates("date", keep="last").set_index("date")
    shared = sorted((d for d in old_by_day.index.intersection(fresh_by_day.index) if d <= j), reverse=True)
    if not shared:
        return "no_common_day", "", ""
    anchor = None
    for d in shared:
        o, f = old_by_day.loc[d], fresh_by_day.loc[d]
        if (np.isclose(float(o["close"]), float(f["close"]), rtol=1e-6, atol=0.0)  # NaN is not close
                and _positive(o["adjusted_close"]) and _positive(f["adjusted_close"])):
            anchor = d
            break
    if anchor is None:
        return "no_same_close_day", "", ""
    ratio = float(fresh_by_day.loc[anchor, "adjusted_close"]) / float(old_by_day.loc[anchor, "adjusted_close"])
    scaled = old[old["date"].astype(str) <= anchor].copy()
    dropped = len(old) - len(scaled)
    keep_flag = scaled["adjusted_close"] == ADJ_CEILING
    new_adj = scaled["adjusted_close"] * ratio
    if bool((~keep_flag & (new_adj >= ADJ_CEILING)).any()):
        return "scaled_past_ceiling", "", ""
    scaled["adjusted_close"] = new_adj.where(~keep_flag, ADJ_CEILING)
    dates = fresh["date"].astype(str)
    tail = fresh[(dates > anchor) & (dates <= day)]
    try:
        joined = pd.concat([scaled, tail[list(old.columns)]], ignore_index=True)
        table = pa.Table.from_pandas(joined, preserve_index=False).cast(pq.read_schema(old_path))
    except (pa.ArrowInvalid, pa.ArrowTypeError, KeyError):  # e.g. a fractional volume after the join day
        return "schema_differs", "", ""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".tmp.{os.getpid()}")
    pq.write_table(table, tmp)
    os.replace(tmp, dest)
    return "", anchor, dropped


def cmd_join(args) -> int:
    store, out = Path(args.store), Path(args.out)
    with open(out / "compare.csv", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames)
        rows = list(reader)
    _check_extend_day(args.extend_to, store, [r["file"] for r in rows if r["decision"] in JOINABLE])
    for col in ("join_note", "join_day", "join_rows_dropped"):
        if col not in fields:
            fields.append(col)
    kept = 0
    joined = 0
    dropped_total = 0
    notes = {}
    for row in rows:
        for col in ("join_note", "join_day", "join_rows_dropped"):
            row.setdefault(col, "")
        if row["decision"] not in JOINABLE:
            continue
        kept += 1
        f = row["file"]
        fresh_path = out / "daily" / f
        if not (store / f).exists() or not fresh_path.exists():
            raise SystemExit(f"join: {f} is missing from the store or from {out / 'daily'}")
        note, join_day, dropped = _join_one(store / f, fresh_path, out / "trimmed" / f, args.extend_to)
        row["join_note"] = note
        row["join_day"] = join_day
        row["join_rows_dropped"] = dropped
        if note:
            notes[note] = notes.get(note, 0) + 1
        else:
            row["decision"] = "join"
            joined += 1
            dropped_total += dropped
    tmp = out / f"compare.csv.tmp.{os.getpid()}"
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, out / "compare.csv")
    print(f"joined {joined} of {kept} kept files")
    print("notes:", notes)
    print(f"old rows dropped after the join day: {dropped_total}")
    return 0


def cmd_apply(args) -> int:
    store, out = Path(args.store), Path(args.out)
    backup = Path(args.backup) if args.backup else None
    compare = pd.read_csv(out / "compare.csv")
    todo = compare[compare["decision"].isin(["replace", "join"])]["file"].tolist()
    if not store.is_dir():
        raise SystemExit(f"store {store} is not a directory")
    if backup is not None:
        backup.mkdir(parents=True, exist_ok=True)
    applied = []
    for f in todo:
        src, dst = out / "trimmed" / f, store / f
        if backup is not None and dst.exists() and not (backup / f).exists():
            shutil.copy2(dst, backup / f)
        tmp = dst.with_name(dst.name + f".tmp.{os.getpid()}")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        applied.append({"file": f, "store": str(store), "at": dt.datetime.now(dt.timezone.utc).isoformat()})
    log = out / "applied.csv"
    new = not log.exists()
    with open(log, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "store", "at"])
        if new:
            w.writeheader()
        w.writerows(applied)
    if backup is None:
        print(f"replaced {len(applied)} files in {store}; no backup kept")
    else:
        print(f"replaced {len(applied)} files in {store}; originals kept in {backup}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m research_lab.fetcher.ops.refetch_flagged_daily")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--scan", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--concurrency", type=int, default=8)
    sel = sub.add_parser("select")
    sel.add_argument("--store", required=True)
    sel.add_argument("--since", required=True)
    sel.add_argument("--out", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--scan", required=True)
    c.add_argument("--store", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--extend-to", default=None)
    jn = sub.add_parser("join")
    jn.add_argument("--store", required=True)
    jn.add_argument("--out", required=True)
    jn.add_argument("--extend-to", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--store", required=True)
    a.add_argument("--out", required=True)
    group = a.add_mutually_exclusive_group(required=True)
    group.add_argument("--backup")
    group.add_argument("--no-backup", action="store_true")
    args = p.parse_args(argv)
    return {"fetch": cmd_fetch, "select": cmd_select, "compare": cmd_compare, "join": cmd_join,
            "apply": cmd_apply}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
