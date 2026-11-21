"""The made-up stocks for the generator v2 test (lane GV2-1, DECISIONS #440 items 1 and 2).

Four groups of six daily stocks: volatile (the capitulation rebound is planted), trending, flat, and twins of the
volatile ones with the rebound removed. Everything here is SYNTHETIC: manifest.json says so, every code starts SYN_.
All random numbers for a code are drawn once, up front, from one seeded generator; every rule below only reads
them, so a rebuild with a deeper drop changes nothing but that drop (plan docs/lanes/GV2-1.md, steps 1 to 6).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from exchange_calendars import get_calendar

from research_lab.synthetic import answer

FIRST_SESSION = "2004-01-02"
LAST_SESSION = "2026-06-30"
EXPECTED_SESSIONS = 5658

# Test design values chosen 2026-10-12 for this test and approved by Tamer at the go (plan, "Rests on").
DESIGN = {
    "N": 20, "W": 2.0, "k": 2.5, "R": 14, "L": 30, "H": 10,
    "drop_days": 3, "D_start": 0.15, "D_step": 0.02, "D_max": 0.29,
    "event_volume_multiple": 3.5, "volume_multiple_step": 0.5, "volume_multiple_max": 6.0,
    "pull_half_life": 5, "pull_days": 20, "pull_noise": 0.01,
    "first_close": 100.0, "base_volume": 1_000_000, "volume_sigma": 0.25,
    "open_sigma_multiple": 0.3, "range_sigma_multiple": 0.5,
    "first_free_sessions": 60, "slot_length": 46, "slot_lead": 20, "slot_jitter_values": 4,
    "spike_step": 1.03, "grind_step": 0.988, "grind_days": 10,
    "walk": {
        "trending": {"mu": 0.0005, "sigma": 0.012},
        "volatile": {"mu": 0.0, "sigma": 0.022},
        "flat": {"mu": 0.0, "sigma": 0.007},
        "twin": {"mu": 0.0, "sigma": 0.022},
    },
}
GROUPS = (("volatile", "VOL"), ("trending", "TRD"), ("flat", "FLT"))
TRAP_KINDS = ("quiet_slide", "up_spike", "slow_grind")
CODES_PER_GROUP = 6


def load_sessions() -> list:
    sessions = get_calendar("XNYS", start="2004-01-01").sessions_in_range(FIRST_SESSION, LAST_SESSION)
    dates = [d.strftime("%Y-%m-%d") for d in sessions]
    if len(dates) != EXPECTED_SESSIONS or dates[0] != FIRST_SESSION or dates[-1] != LAST_SESSION:
        raise RuntimeError(f"XNYS sessions {FIRST_SESSION}..{LAST_SESSION}: expected {EXPECTED_SESSIONS}, got {len(dates)} "
                           f"({dates[:1]}..{dates[-1:]})")
    return dates


def slot_count(n: int) -> int:
    return max(0, (n - DESIGN["first_free_sessions"]) // DESIGN["slot_length"])


def draw_arrays(seed: int, code_number: int, n: int) -> dict:
    """Every random number a code uses, drawn once and in a fixed order."""
    rng = np.random.default_rng([seed, code_number])
    slots = slot_count(n)
    return {
        "z_close": rng.standard_normal(n), "z_vol": rng.standard_normal(n), "z_open": rng.standard_normal(n),
        "z_high": rng.standard_normal(n), "z_low": rng.standard_normal(n), "z_pull": rng.standard_normal(n),
        "offset": rng.integers(0, DESIGN["slot_jitter_values"], slots), "shuffle": rng.permutation(slots),
    }


def plan_events(group: str, arrays: dict) -> list:
    """The slots of a volatile, trending or flat code: which slot holds which event kind."""
    slots = len(arrays["shuffle"])
    half = slots // 2
    order = [int(s) for s in arrays["shuffle"]]
    events = []
    if group == "volatile":
        for slot in order[:half]:
            events.append({"kind": "planted", "slot": slot})
        rest = order[half:]
    else:
        rest = order[:half]
    for i, slot in enumerate(rest):
        events.append({"kind": TRAP_KINDS[i % 3], "slot": slot})
    for ev in events:
        ev["start_index"] = (DESIGN["first_free_sessions"] + ev["slot"] * DESIGN["slot_length"]
                             + DESIGN["slot_lead"] + int(arrays["offset"][ev["slot"]]))
    return sorted(events, key=lambda e: e["start_index"])


def _span(kind: str, start: int) -> "tuple[int, int, int]":
    """(first index, last index, event day index) an event covers."""
    if kind in ("planted", "twin_drop"):
        return start, start + DESIGN["drop_days"] + DESIGN["pull_days"] - 1, start + DESIGN["drop_days"] - 1
    if kind == "quiet_slide":
        return start, start + DESIGN["drop_days"] - 1, start + DESIGN["drop_days"] - 1
    if kind == "up_spike":
        return start, start, start
    if kind == "slow_grind":
        return start, start + DESIGN["grind_days"] - 1, start + DESIGN["grind_days"] - 1
    if kind == "dropped":
        return start, start + DESIGN["drop_days"] + DESIGN["pull_days"] - 1, start + DESIGN["drop_days"] - 1
    raise ValueError(f"unknown event kind {kind!r}")


def build_series(arrays: dict, group: str, events: list, n: int) -> pd.DataFrame:
    """The price frame (without dates) for the first n sessions. `events` carry kind, start_index and, for drops, D.

    Kinds: planted (drop then pull back), twin_drop (the drop only), quiet_slide, up_spike, slow_grind, dropped (nothing)."""
    walk = DESIGN["walk"][group]
    mu, sigma = walk["mu"], walk["sigma"]
    steps = mu + sigma * arrays["z_close"][:n]
    logc = np.empty(n)
    vol_mult = np.ones(n)
    phi = 0.5 ** (1.0 / DESIGN["pull_half_life"])
    logc[0] = math.log(DESIGN["first_close"])
    filled = 1  # logc[:filled] is final

    def walk_to(stop: int) -> None:
        nonlocal filled
        stop = min(stop, n)
        if stop > filled:
            logc[filled:stop] = logc[filled - 1] + np.cumsum(steps[filled:stop])
            filled = stop

    for ev in sorted(events, key=lambda e: e["start_index"]):
        s, kind = ev["start_index"], ev["kind"]
        if s >= n or kind == "dropped":
            continue
        walk_to(s)
        base = logc[s - 1]
        if kind in ("planted", "twin_drop", "quiet_slide"):
            d = ev["D"] if kind != "quiet_slide" else DESIGN["D_start"]
            step = math.log(1.0 - d) / DESIGN["drop_days"]
            for i in range(DESIGN["drop_days"]):
                if s + i < n:
                    logc[s + i] = base + (i + 1) * step
            end = min(s + DESIGN["drop_days"], n)
            filled = max(filled, end)
            if kind != "quiet_slide" and s + DESIGN["drop_days"] - 1 < n:
                vol_mult[s + DESIGN["drop_days"] - 1] = ev.get("mult", DESIGN["event_volume_multiple"])
            if kind == "planted":
                x = logc[end - 1] if end - 1 >= s else base
                pull_end = min(s + DESIGN["drop_days"] + DESIGN["pull_days"], n)
                for t in range(end, pull_end):
                    x = phi * x + (1.0 - phi) * base + DESIGN["pull_noise"] * arrays["z_pull"][t]
                    logc[t] = x
                filled = max(filled, pull_end)
        elif kind == "up_spike":
            logc[s] = base + math.log(DESIGN["spike_step"])
            vol_mult[s] = DESIGN["event_volume_multiple"]
            filled = s + 1
        elif kind == "slow_grind":
            stop = min(s + DESIGN["grind_days"], n)
            logc[s:stop] = base + np.arange(1, stop - s + 1) * math.log(DESIGN["grind_step"])
            filled = stop
        else:
            raise ValueError(f"unknown event kind {kind!r}")
    walk_to(n)

    close = np.exp(logc)
    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1] * np.exp(DESIGN["open_sigma_multiple"] * sigma * arrays["z_open"][1:n])
    high = np.maximum(open_, close) * np.exp(DESIGN["range_sigma_multiple"] * sigma * np.abs(arrays["z_high"][:n]))
    low = np.minimum(open_, close) * np.exp(-DESIGN["range_sigma_multiple"] * sigma * np.abs(arrays["z_low"][:n]))
    volume = np.maximum(1, np.rint(DESIGN["base_volume"] * np.exp(DESIGN["volume_sigma"] * arrays["z_vol"][:n]) * vol_mult))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "adjusted_close": close.copy(), "volume": volume.astype("int64")})


def _failing(frame: pd.DataFrame, index: int) -> list:
    """Names of the conditions not met at one session, by answer.py's own functions."""
    d = DESIGN
    close = frame["close"].astype(float)
    volume = frame["volume"].astype(float)
    checks = {
        "band": close < answer.lower_band(close, d["N"], d["W"]),
        "volume": answer.volume_ratio(volume, d["N"]) >= d["k"],
        "rsi": answer.rsi(close, d["R"]) < d["L"],
    }
    return [name for name, ok in checks.items() if not bool(ok.iloc[index])]


def _ladders() -> "tuple[list, list]":
    n_d = int(round((DESIGN["D_max"] - DESIGN["D_start"]) / DESIGN["D_step"])) + 1
    n_m = int(round((DESIGN["volume_multiple_max"] - DESIGN["event_volume_multiple"]) / DESIGN["volume_multiple_step"])) + 1
    return ([round(DESIGN["D_start"] + i * DESIGN["D_step"], 10) for i in range(n_d)],
            [round(DESIGN["event_volume_multiple"] + i * DESIGN["volume_multiple_step"], 10) for i in range(n_m)])


def deepen(arrays: dict, events: list, group: str = "volatile") -> list:
    """Step 5: build forward in time. A drop event that fails a condition is rebuilt on the same draws: a volume ratio
    miss raises the event-day volume multiple, a band or RSI miss deepens the drop. Events start from their own D and
    multiple (a twin starts from its SYN_VOL event's final ones). Still failing at the top of a ladder: ordinary
    walk, kind 'dropped', with the failing conditions listed."""
    d_ladder, m_ladder = _ladders()
    final = []
    for ev in events:
        ev = dict(ev)
        if ev["kind"] in ("planted", "twin_drop"):
            day = ev["start_index"] + DESIGN["drop_days"] - 1
            i_d = d_ladder.index(ev.get("D", DESIGN["D_start"]))
            i_m = m_ladder.index(ev.get("mult", DESIGN["event_volume_multiple"]))
            while True:
                ev["D"], ev["mult"] = d_ladder[i_d], m_ladder[i_m]
                failing = _failing(build_series(arrays, group, final + [ev], day + 1), day)
                if not failing:
                    break
                progressed = False
                if "volume" in failing and i_m < len(m_ladder) - 1:
                    i_m += 1
                    progressed = True
                if ("band" in failing or "rsi" in failing) and i_d < len(d_ladder) - 1:
                    i_d += 1
                    progressed = True
                if not progressed:
                    ev["failing"] = failing
                    ev["kind"] = "dropped"
                    ev["D_tried_up_to"], ev["mult_tried_up_to"] = ev.pop("D"), ev.pop("mult")
                    break
        final.append(ev)
    return final


def _describe(events: list, dates: list, frame: pd.DataFrame) -> list:
    flags = answer.signal_days(frame, DESIGN["N"], DESIGN["W"], DESIGN["k"], DESIGN["R"], DESIGN["L"])
    out = []
    for ev in events:
        first, last, day = _span(ev["kind"], ev["start_index"])
        last = min(last, len(dates) - 1)
        row = {k: v for k, v in ev.items()}
        row.update(start_date=dates[first], end_index=last, end_date=dates[last], event_index=day, event_date=dates[day])
        if ev["kind"] in TRAP_KINDS:
            row["meets_all"] = bool(flags.iloc[first:last + 1].any())
        out.append(row)
    return out


def make(out_dir: Path, seed: int) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    dates = load_sessions()
    n = len(dates)
    codes: dict = {}
    frames: dict = {}
    number = 0
    vol_arrays: dict = {}
    vol_events: dict = {}
    for group, tag in GROUPS:
        for i in range(1, CODES_PER_GROUP + 1):
            number += 1
            code = f"SYN_{tag}_{i:02d}"
            arrays = draw_arrays(seed, number, n)
            events = deepen(arrays, plan_events(group, arrays)) if group == "volatile" else plan_events(group, arrays)
            frame = build_series(arrays, group, events, n)
            frames[code] = frame
            codes[code] = {"group": group, "code_number": number, "events": _describe(events, dates, frame)}
            if group == "volatile":
                vol_arrays[i], vol_events[i] = arrays, events
    for i in range(1, CODES_PER_GROUP + 1):
        code = f"SYN_TWN_{i:02d}"
        twin_events = []
        for ev in vol_events[i]:
            ev = dict(ev)
            if ev["kind"] == "planted":
                ev["kind"] = "twin_drop"
            twin_events.append(ev)
        twin_events = deepen(vol_arrays[i], twin_events, "twin")
        frame = build_series(vol_arrays[i], "twin", twin_events, n)
        frames[code] = frame
        codes[code] = {"group": "twin", "twin_of": f"SYN_VOL_{i:02d}", "code_number": i,
                       "events": _describe(twin_events, dates, frame)}
    for code, frame in frames.items():
        frame.insert(0, "date", dates)
        frame.to_parquet(out_dir / f"{code}.parquet", index=False)
    manifest = {"synthetic": True, "seed": seed, "design": DESIGN, "sessions": n,
                "first_session": dates[0], "last_session": dates[-1], "codes": codes}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


def twin_events(seed: int, i: int, n: int) -> "tuple[dict, list]":
    """(arrays, events) of SYN_TWN_i exactly as `make` builds it."""
    arrays = draw_arrays(seed, i, n)
    vol = deepen(arrays, plan_events("volatile", arrays))
    events = []
    for ev in vol:
        ev = dict(ev)
        if ev["kind"] == "planted":
            ev["kind"] = "twin_drop"
        events.append(ev)
    return arrays, deepen(arrays, events, "twin")


def _event_length(kind: str) -> int:
    if kind in ("twin_drop", "quiet_slide"):
        return DESIGN["drop_days"]
    if kind == "up_spike":
        return 1
    if kind == "slow_grind":
        return DESIGN["grind_days"]
    raise ValueError(f"no length for event kind {kind!r}")


def random_twin_events(events: list, seed: int, i: int, n: int, timing_seed: "int | None" = None) -> list:
    """The same events in a random order at uniformly random, non-overlapping places (a count-conditioned Poisson
    process), so the gap after one drop says nothing about the next. Events of kind dropped build nothing and are left out."""
    timing_seed = seed if timing_seed is None else timing_seed
    kept = [ev for ev in events if ev["kind"] != "dropped"]
    rng = np.random.default_rng([timing_seed, 1000 + i])
    placed = [kept[p] for p in rng.permutation(len(kept))]
    lengths = [_event_length(ev["kind"]) for ev in placed]
    first = DESIGN["first_free_sessions"]
    free = n - first - sum(lengths)
    if free < 0:
        raise RuntimeError(f"{len(placed)} events need {sum(lengths)} sessions, only {n - first} are free")
    u = np.sort(rng.integers(0, free + 1, len(placed)))
    out = []
    used = 0
    for j, ev in enumerate(placed):
        new = {"kind": ev["kind"], "start_index": int(first + u[j] + used)}
        if ev["kind"] == "twin_drop":
            new["D"], new["mult"] = ev["D"], ev["mult"]
        out.append(new)
        used += lengths[j]
    return out


def make_random_twins(out_dir: Path, seed: int, timing_seed: "int | None" = None) -> dict:
    timing_seed = seed if timing_seed is None else timing_seed
    out_dir.mkdir(parents=True, exist_ok=True)
    dates = load_sessions()
    n = len(dates)
    codes: dict = {}
    frames: dict = {}
    for i in range(1, CODES_PER_GROUP + 1):
        code = f"SYN_TWR_{i:02d}"
        arrays, events = twin_events(seed, i, n)
        placed = random_twin_events(events, seed, i, n, timing_seed)
        frame = build_series(arrays, "twin", placed, n)
        flags = answer.signal_days(frame, DESIGN["N"], DESIGN["W"], DESIGN["k"], DESIGN["R"], DESIGN["L"])
        rows = _describe(placed, dates, frame)
        for row in rows:
            if row["kind"] == "twin_drop":
                row["meets_all"] = bool(flags.iloc[row["event_index"]])
        frames[code] = frame
        codes[code] = {"group": "twin_random", "twin_of": f"SYN_VOL_{i:02d}", "same_drops_as": f"SYN_TWN_{i:02d}",
                       "code_number": i, "events": rows}
    for code, frame in frames.items():
        frame.insert(0, "date", dates)
        frame.to_parquet(out_dir / f"{code}.parquet", index=False)
    manifest = {"synthetic": True, "set": "twin_random_timing", "seed": seed, "design": DESIGN,
                "timing": {"rule": "uniform_no_overlap", "rng": "default_rng([timing_seed, 1000 + i])",
                           "timing_seed": timing_seed,
                           "lengths": {"twin_drop": DESIGN["drop_days"], "quiet_slide": DESIGN["drop_days"],
                                       "up_spike": 1, "slow_grind": DESIGN["grind_days"]}},
                "sessions": n, "first_session": dates[0], "last_session": dates[-1], "codes": codes}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest
