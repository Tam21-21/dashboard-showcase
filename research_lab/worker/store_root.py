"""Resolves the single, consolidated EODHD price-store root the research code reads (M5 fetcher job
10, docs/handoffs/2026-09-04_fetcher/10_job_reader_and_provenance.md, AC-1).

Three copies of the EODHD store exist today. The external SSD (/mnt/ssd/eodhd/raw) is the fetcher's
own source of truth and is never opened by this module. Two copies live on the laptop itself: an OLD
two-grain copy (~/eodhd, holding only 1h/ and daily/, frozen since 2026-08-11, the copy every
research read used to resolve to) and a COMPLETE five-grain replica (~/market_data/eodhd/raw, holding
daily/, 1h/, 5min/, 1min/, bulk/ as direct child directories). The replica's completeness is verified
2026-09-04 two ways: `~/finish_eodhd_copy.log`'s own rsync report ("ALL GRAINS MATCH -- the laptop
copy is complete", every one of the five grain file counts equal to the SSD source's own count at
that time), and a direct byte-for-byte `diff` of several tickers (AAPL, MSFT, NVDA, TSLA) between the
old copy and the replica's own overlapping 1h/daily files, all identical (exit 0). This module's
default now points at that complete replica, so the research path stops silently reading the stale
two-grain copy without every caller needing to change by hand -- the design constraint this job names
directly: "mount the store ROOT once and let the grain select inside it."

Kept apart from helpers.py (the same reasoning pathmap.py's own module docstring already gives for
itself): pure pathlib and os, no httpx, no required env read at import time, so a plain-interpreter
probe can exercise it without needing RESEARCH_WORKER_API_KEY / RESEARCH_SERVER_URL set first.
"""
from __future__ import annotations

import os
from pathlib import Path

# The single consolidated store root, holding every grain we own as a direct child directory (daily/,
# 1h/, 5min/, 1min/, bulk/). Deliberately distinct from BOTH old paths this replaces: the frozen
# two-grain copy's own root, and the even narrower single-grain daily-only path fetch_dataset.py used
# to mount before this module existed (see this unit's own build report for the exact old strings,
# never repeated here so this file's own text cannot itself be mistaken for a live reference to them).
# expanduser keeps this one line the only place a hardcoded username appears in this module.
_EODHD_STORE_ROOT_DEFAULT = "~/market_data/eodhd/raw"


def resolve_eodhd_store_root() -> Path:
    """Return the host path of the consolidated EODHD store, the one location holding every grain.

    EODHD_STORE_ROOT overrides the default when set, read at CALL time (not frozen at import), so a
    probe pointing at a temp fixture directory just sets the env var before calling, and a running
    worker process picks up an operator's env change on its next job rather than needing a restart.
    An explicit override is used exactly as given, with no expanduser applied to it: the caller who
    sets it controls its own shape (an absolute path from a probe fixture needs no expansion), the
    same convention fetch_dataset.py's own former daily-only override used before this module
    replaced it.

    RA-5 gate-review SHOULD-FIX 5 (2026-09-04): `os.environ.get(key, default)` falls back to
    `default` only when `key` is ABSENT from the environment; an explicitly blank override
    (`EODHD_STORE_ROOT=""`) is a PRESENT key holding an empty string, so `.get()` returned `""`
    unchanged and this function used to hand back `Path("")`, which is `PosixPath('.')` -- the
    process's own current working directory. That silently defeated fetch_dataset.py's own fail-loud
    `.is_dir()` misconfiguration guard (added specifically to make a bad EODHD_STORE_ROOT
    debuggable): a process's own cwd always exists, so `.is_dir()` never caught it, and the worker
    would bind-mount whatever the cwd happened to be as if it were the eodhd store. Raising loud here
    instead, at the resolver itself where the misconfiguration actually originates, rather than
    letting it travel downstream disguised as a valid path.
    """
    raw_override = os.environ.get("EODHD_STORE_ROOT")
    if raw_override is not None and raw_override.strip() == "":
        raise ValueError(
            "EODHD_STORE_ROOT is set but blank; refusing to silently resolve the eodhd store to "
            "the current working directory. Either unset EODHD_STORE_ROOT to use the default "
            f"({_EODHD_STORE_ROOT_DEFAULT}) or set it to a real store path."
        )
    return Path(raw_override if raw_override is not None else os.path.expanduser(_EODHD_STORE_ROOT_DEFAULT))


# Every grain the resolved store is expected to hold. RA-5 gate-review SHOULD-FIX 8, CORRECTED
# 2026-09-04: an earlier draft of this comment claimed the old research_lab/CLAUDE.md data/ table (archived at WF1b)
# "names the same five" grains; that row actually reads, byte for byte (`grep -n "Local price
# parquets under" docs/archive/old_workflow/area_maps/research_lab_CLAUDE.md` -> line 46), "Local price parquets under `alpaca_sip/`
# and `eodhd/`, one folder per ticker per timeframe." -- it names zero grains by name, so that
# citation was false and is removed (root-cause dispatch, 2026-09-04, root-fixing the SAME defect
# class this correction itself had re-introduced: the first correction's own quote dropped the
# row's backticks and trailing period, substance right, bytes wrong, which is why this one is
# quoted with a re-runnable grep beside it rather than retyped from memory again).
# The tuple itself is independently correct, verified 2026-09-04 directly
# against the real replica's own five subdirectories (`ls -1 ~/market_data/eodhd/raw`: 1h, 1min,
# 5min, bulk, daily). Used only by the completeness status check below; resolve_eodhd_store_root
# itself stays agnostic to which grains actually exist under whatever root it returns.
# Lane D2b (2026-10-11) adds 1h_joined: the hourly series joined from the built files and the native hourly
# files (research_lab/fetcher/join_1h.py). Nothing reads it yet (lane D2c switches the hourly reader); the grain is listed so a store without the joined series shows as incomplete.
_EXPECTED_GRAINS = ("daily", "1h", "1h_joined", "5min", "1min", "bulk")


def check_store_grains(store_root: Path) -> dict:
    """Ops monitor status (M5 fetcher job 10 what-can-break mode 1: "The newly resolved store is
    missing a grain or a file the old copy had, so a run that worked yesterday fails today"). Returns
    whether every expected grain directory exists directly under store_root, so a missing grain
    surfaces here as a status rather than only as a deep FileNotFoundError the first symbol that needs
    it happens to hit.

    A grain directory existing says nothing about whether every file inside it is present or correct:
    this is a coarse, cheap completeness check (five is_dir calls), not a per-file audit.
    """
    missing = [g for g in _EXPECTED_GRAINS if not (store_root / g).is_dir()]
    return {"complete": not missing, "missing_grains": missing, "expected_grains": list(_EXPECTED_GRAINS)}


def report_store_grains(store_root: Path) -> None:
    """Prints the grain-completeness STATUS on every call, not fire-only-when-broken: this is a
    status check ("reported as a status rather than raised only on first use"), not a defect count,
    so a healthy resolution is worth seeing in the log too, not only a broken one.
    """
    status = check_store_grains(store_root)
    print(f"store_root: resolved store holds every expected grain: {status['complete']} "
          f"(missing={status['missing_grains']})", flush=True)


# Lane GV2-4a: the made-up stocks of research_lab/synthetic (DECISIONS #440). They live in their own store,
# outside the repo and apart from the EODHD store, so a synthetic file can never sit among real prices.
_SYNTHETIC_STORE_ROOT_DEFAULT = "~/market_data/synthetic/gv2"


def resolve_synthetic_store_root() -> Path:
    """Return the host folder holding the made-up stocks (one <code>.parquet each, plus manifest.json).

    SYNTHETIC_STORE_ROOT overrides the default, read at CALL time so a worker picks up a change on its next
    job. An explicit override is used exactly as given. A blank override is refused, the same reason
    resolve_eodhd_store_root refuses one: Path("") is the current folder and would be read as the store.
    """
    raw_override = os.environ.get("SYNTHETIC_STORE_ROOT")
    if raw_override is not None and raw_override.strip() == "":
        raise ValueError(
            "SYNTHETIC_STORE_ROOT is set but blank; refusing to silently resolve the synthetic store to "
            "the current working directory. Either unset SYNTHETIC_STORE_ROOT to use the default "
            f"({_SYNTHETIC_STORE_ROOT_DEFAULT}) or set it to a real store path."
        )
    return Path(raw_override if raw_override is not None else os.path.expanduser(_SYNTHETIC_STORE_ROOT_DEFAULT))
