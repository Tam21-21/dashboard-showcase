"""The made-up stocks, version 2 (lane GV3-D1, DECISIONS #467 item 9), built beside version 1 (make.py, untouched).

Four groups of nine daily stocks: volatile, trending, flat, and twins of the volatile ones. Differences from version 1:
the planted bounce ends at day 10 and then fades; a twin drop recovers to where it started, so twins stay above $1;
and the volatile stocks hold two kinds of decoy crash: one that only the Donchian versions of the rebound template
catch (the close is not under the Bollinger lower band) and one that only the Stochastic versions catch (the RSI
stays at or above its line). Decoys never bounce. Everything here is SYNTHETIC: manifest.json says so.
All random numbers for a code are drawn once, up front, from one seeded generator (plan docs/lanes/GV3-D1.md).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from research_lab.synthetic import answer
from research_lab.synthetic.make import DESIGN, GROUPS, TRAP_KINDS, load_sessions

# Version 1 values this version reuses unchanged, read from make.DESIGN (so a v1 edit shows up here by name).
V1_SHARED = ("first_close", "base_volume", "volume_sigma", "open_sigma_multiple", "range_sigma_multiple",
             "first_free_sessions", "slot_jitter_values", "spike_step", "grind_step", "grind_days", "D_start",
             "D_step", "D_max", "event_volume_multiple", "volume_multiple_step", "volume_multiple_max", "drop_days",
             "N", "W", "k", "R", "L")

# Test design values for this test, chosen 2026-10-17 in the planning session and approved by Tamer at the go (plan, "Rests on").
DESIGN_V2 = {
    **{key: DESIGN[key] for key in V1_SHARED},
    "walk": DESIGN["walk"],
    "H": 10,                    # the rebound template's max hold pick (GV3-T ranges), tested at max hold only
    "donchian_window": 20,      # on the template's N letter, shared with the Bollinger window (GV3-T)
    "stoch_window": 14,         # %K window pick SK (GV3-T)
    "stoch_line": 20,           # %K line pick SL (GV3-T)
    "codes_per_group": 9,       # nine codes per group so six searched codes give over 100 trades (planning session)
    "slot_length": 60,          # a decoy and its recovery fit one slot (planning session)
    "slot_lead": 10,            # sessions before the event start inside a slot (planning session)
    "planted_slots": 30, "decoy_donchian_slots": 30, "decoy_stochastic_slots": 30,  # 93 slots: 30, 30, 30 and one of each trap
    "bounce_half_life": 3,      # the bounce ends at day 10 (DECISIONS #467 item 9)
    "bounce_days": 10, "fade_days": 10,
    "fade_fraction": 0.6,       # the fade gives back this share of the bounce (planning session)
    "pull_noise": 0.004,        # daily noise on the planted and decoy paths (planning session)
    "flat_until": 25,           # planted: last flat session after the start, then the recovery (planning session)
    "twin_flat_until": 27,      # twin drop: last flat session after the start, then the recovery (planning session)
    "recovery_days": 15,        # linear return to the start level, so twins stay above $1 (DECISIONS #467 item 9)
    "slide_days": 15, "g_start": 0.012, "g_step": 0.004, "g_max": 0.04,  # decoy_donchian slide per day (planning session)
    "rise_days": 20, "rise_step": 0.006,                                  # decoy_stochastic run-up (planning session)
    "X_start": 0.12, "X_step": 0.02, "X_max": 0.30,                       # decoy_stochastic one-day drop (planning session)
    "decoy_down_days": 20, "decoy_down_step": 0.004,                      # decoys keep falling, they never bounce
    "decoy_back_days": 12,                                                # then return to the start level
    "trend_rise_days": 80,      # a planted trend cycle rises for 80 sessions (planning session 2026-10-20)
    "trend_fall_days": 40,      # then falls back to the start in 40 (planning session 2026-10-20)
    "trend_rise": 0.35,         # the rise is 35% (planning session 2026-10-20)
    "trend_noise": DESIGN["walk"]["trending"]["sigma"],  # daily noise on the trend path: the trending walk's own sigma (planning session)
    "trend_phase_step": 13,     # code i of a trend group starts its cycles 13 (i - 1) sessions later (planning session)
    "trend_fast": 10,           # average-crossover fast window pick F (GV3-T ranges)
    "trend_slow": 50,           # average-crossover slow window pick TS (GV3-T ranges)
}
VOL_PREFIX = {"volatile": "VOL", "trending": "TRD", "flat": "FLT"}
TREND_KINDS = {"trending": "planted_trend", "trend_twin": "twin_trend"}
KINDS_DEEPENED = {"planted": ("D", "D_start", "D_step", "D_max"),
                  "twin_drop": ("D", "D_start", "D_step", "D_max"),
                  "decoy_donchian": ("g", "g_start", "g_step", "g_max"),
                  "decoy_stochastic": ("X", "X_start", "X_step", "X_max")}
# Conditions whose miss raises the event's own severity, and those that drop the event at once.
RAISE = {"planted": ("band", "rsi"), "twin_drop": ("band", "rsi"), "decoy_donchian": ("donchian", "rsi"),
         "decoy_stochastic": ("band", "stoch")}
DROP_AT_ONCE = {"planted": (), "twin_drop": (), "decoy_donchian": ("band_hit",), "decoy_stochastic": ("rsi_low",)}


def slot_count(n: int) -> int:
    return max(0, (n - DESIGN_V2["first_free_sessions"]) // DESIGN_V2["slot_length"])


def draw_arrays(seed: int, code_number: int, n: int) -> dict:
    """Every random number a code uses, drawn once and in v1's order."""
    rng = np.random.default_rng([seed, code_number])
    slots = slot_count(n)
    return {
        "z_close": rng.standard_normal(n), "z_vol": rng.standard_normal(n), "z_open": rng.standard_normal(n),
        "z_high": rng.standard_normal(n), "z_low": rng.standard_normal(n), "z_pull": rng.standard_normal(n),
        "offset": rng.integers(0, DESIGN_V2["slot_jitter_values"], slots), "shuffle": rng.permutation(slots),
    }


def plan_events(group: str, arrays: dict, phase: int = 0) -> list:
    """Which slot holds which event kind. A trend group (trending, trend_twin) holds back-to-back cycles from
    first_free_sessions + phase, one every trend_rise_days + trend_fall_days sessions."""
    d = DESIGN_V2
    if group in TREND_KINDS:
        first = d["first_free_sessions"] + phase
        cycle = d["trend_rise_days"] + d["trend_fall_days"]
        count = (len(arrays["z_close"]) - first) // cycle
        return [{"kind": TREND_KINDS[group], "slot": k, "start_index": first + k * cycle} for k in range(count)]
    order = [int(s) for s in arrays["shuffle"]]
    events = []
    if group == "volatile":
        a = d["planted_slots"]
        b = a + d["decoy_donchian_slots"]
        c = b + d["decoy_stochastic_slots"]
        events += [{"kind": "planted", "slot": s} for s in order[:a]]
        events += [{"kind": "decoy_donchian", "slot": s} for s in order[a:b]]
        events += [{"kind": "decoy_stochastic", "slot": s} for s in order[b:c]]
        events += [{"kind": TRAP_KINDS[i], "slot": s} for i, s in enumerate(order[c:])]
    else:
        for i, slot in enumerate(order[:len(order) // 2]):
            events.append({"kind": TRAP_KINDS[i % 3], "slot": slot})
    for ev in events:
        ev["start_index"] = (d["first_free_sessions"] + ev["slot"] * d["slot_length"]
                             + d["slot_lead"] + int(arrays["offset"][ev["slot"]]))
    return sorted(events, key=lambda e: e["start_index"])


def _span(kind: str, start: int) -> "tuple[int, int, int]":
    """(first index, last index, signal day index) an event covers."""
    d = DESIGN_V2
    if kind in ("planted", "twin_drop"):
        flat = d["flat_until"] if kind == "planted" else d["twin_flat_until"]
        return start, start + flat + d["recovery_days"], start + d["drop_days"] - 1
    if kind == "decoy_donchian":
        last = start + d["slide_days"] + d["decoy_down_days"] + d["decoy_back_days"] - 1
        return start, last, start + d["slide_days"] - 1
    if kind == "decoy_stochastic":
        last = start + d["rise_days"] + 1 + d["decoy_down_days"] + d["decoy_back_days"] - 1
        return start, last, start + d["rise_days"]
    if kind in ("planted_trend", "twin_trend"):
        return start, start + d["trend_rise_days"] + d["trend_fall_days"] - 1, start
    if kind == "quiet_slide":
        return start, start + d["drop_days"] - 1, start + d["drop_days"] - 1
    if kind == "up_spike":
        return start, start, start
    if kind == "slow_grind":
        return start, start + d["grind_days"] - 1, start + d["grind_days"] - 1
    raise ValueError(f"unknown event kind {kind!r}")


def build_series(arrays: dict, group: str, events: list, n: int) -> pd.DataFrame:
    """The price frame (without dates) for the first n sessions. Events that are 'dropped' leave the ordinary walk."""
    d = DESIGN_V2
    walk = d["walk"]["trending" if group == "trend_twin" else group]
    mu, sigma = walk["mu"], walk["sigma"]
    steps = mu + sigma * arrays["z_close"][:n]
    zp = arrays["z_pull"]
    noise = d["pull_noise"]
    logc = np.empty(n)
    vol_mult = np.ones(n)
    phi = 0.5 ** (1.0 / d["bounce_half_life"])
    logc[0] = math.log(d["first_close"])
    state = {"filled": 1}  # logc[:filled] is final

    def walk_to(stop: int) -> None:
        stop = min(stop, n)
        if stop > state["filled"]:
            logc[state["filled"]:stop] = logc[state["filled"] - 1] + np.cumsum(steps[state["filled"]:stop])
            state["filled"] = stop

    def put(start: int, values: list) -> None:
        stop = min(start + len(values), n)
        if stop > start:
            logc[start:stop] = values[:stop - start]
        state["filled"] = max(state["filled"], stop)

    def drift(x: float, t0: int, count: int, step: float) -> "tuple[list, float]":
        out = []
        for t in range(t0, t0 + count):
            x = x + step + noise * zp[t]
            out.append(x)
        return out, x

    def linear_back(x: float, base: float, count: int) -> list:
        return [x + (base - x) * i / count for i in range(1, count + 1)]

    for ev in sorted(events, key=lambda e: e["start_index"]):
        s, kind = ev["start_index"], ev["kind"]
        if s >= n or kind == "dropped":
            continue
        if s < state["filled"]:
            raise RuntimeError(f"event {kind} at session {s} starts before session {state['filled']} is final")
        walk_to(s)
        base = logc[s - 1]
        signal = _span(kind, s)[2]
        if kind in ("planted", "twin_drop", "quiet_slide"):
            dd = ev["D"] if kind != "quiet_slide" else d["D_start"]
            step = math.log(1.0 - dd) / d["drop_days"]
            seg = [base + (i + 1) * step for i in range(d["drop_days"])]
            bottom = seg[-1]
            x = bottom
            t = s + d["drop_days"]
            if kind == "planted":
                for _ in range(d["bounce_days"]):
                    x = phi * x + (1.0 - phi) * base + noise * zp[t]
                    seg.append(x)
                    t += 1
                top = x
                for i in range(1, d["fade_days"] + 1):
                    x = top - d["fade_fraction"] * (top - bottom) * i / d["fade_days"] + noise * zp[t]
                    seg.append(x)
                    t += 1
                flat, x = drift(x, t, s + d["flat_until"] - t + 1, 0.0)
                seg += flat
                seg += linear_back(x, base, d["recovery_days"])
            elif kind == "twin_drop":
                flat, x = drift(x, t, s + d["twin_flat_until"] - t + 1, 0.0)
                seg += flat
                seg += linear_back(x, base, d["recovery_days"])
            put(s, seg)
            if kind != "quiet_slide" and signal < n:
                vol_mult[signal] = ev.get("mult", d["event_volume_multiple"])
        elif kind == "decoy_donchian":
            seg, x = drift(base, s, d["slide_days"], -ev["g"])
            down, x = drift(x, s + d["slide_days"], d["decoy_down_days"], -d["decoy_down_step"])
            put(s, seg + down + linear_back(x, base, d["decoy_back_days"]))
            if signal < n:
                vol_mult[signal] = ev["mult"]
        elif kind == "decoy_stochastic":
            seg, x = drift(base, s, d["rise_days"], d["rise_step"])
            x = x + math.log(1.0 - ev["X"]) + noise * zp[s + d["rise_days"]]
            seg.append(x)
            down, x = drift(x, s + d["rise_days"] + 1, d["decoy_down_days"], -d["decoy_down_step"])
            put(s, seg + down + linear_back(x, base, d["decoy_back_days"]))
            if signal < n:
                vol_mult[signal] = ev["mult"]
        elif kind in ("planted_trend", "twin_trend"):
            up = math.log(1.0 + d["trend_rise"]) / d["trend_rise_days"] if kind == "planted_trend" else 0.0
            down = -math.log(1.0 + d["trend_rise"]) / d["trend_fall_days"] if kind == "planted_trend" else 0.0
            x = base
            seg = []
            for j in range(d["trend_rise_days"] + d["trend_fall_days"]):
                x = x + (up if j < d["trend_rise_days"] else down) + d["trend_noise"] * zp[s + j]
                seg.append(x)
            put(s, seg)
        elif kind == "up_spike":
            put(s, [base + math.log(d["spike_step"])])
            vol_mult[s] = d["event_volume_multiple"]
        elif kind == "slow_grind":
            put(s, [base + (i + 1) * math.log(d["grind_step"]) for i in range(d["grind_days"])])
        else:
            raise ValueError(f"unknown event kind {kind!r}")
    walk_to(n)

    close = np.exp(logc)
    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1] * np.exp(d["open_sigma_multiple"] * sigma * arrays["z_open"][1:n])
    high = np.maximum(open_, close) * np.exp(d["range_sigma_multiple"] * sigma * np.abs(arrays["z_high"][:n]))
    low = np.minimum(open_, close) * np.exp(-d["range_sigma_multiple"] * sigma * np.abs(arrays["z_low"][:n]))
    volume = np.maximum(1, np.rint(d["base_volume"] * np.exp(d["volume_sigma"] * arrays["z_vol"][:n]) * vol_mult))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "adjusted_close": close.copy(), "volume": volume.astype("int64")})


def _failing(frame: pd.DataFrame, index: int, kind: str) -> list:
    """Names of the conditions an event's signal day does not meet, by answer.py's own functions."""
    d = DESIGN_V2
    close = frame["close"].astype(float)
    low = frame["low"].astype(float)
    high = frame["high"].astype(float)
    volume = frame["volume"].astype(float)
    band = close < answer.lower_band(close, d["N"], d["W"])
    vol = answer.volume_ratio(volume, d["N"]) >= d["k"]
    rsi_low = answer.rsi(close, d["R"]) < d["L"]
    if kind in ("planted", "twin_drop"):
        checks = {"band": band, "volume": vol, "rsi": rsi_low}
    elif kind == "decoy_donchian":
        checks = {"donchian": close < answer.donchian_low(low, d["donchian_window"]), "rsi": rsi_low, "volume": vol,
                  "band_hit": ~band}
    elif kind == "decoy_stochastic":
        checks = {"band": band, "stoch": answer.stochastic_k(high, low, close, d["stoch_window"]) < d["stoch_line"],
                  "volume": vol, "rsi_low": ~rsi_low}
    else:
        raise ValueError(f"no conditions for kind {kind!r}")
    return [name for name, ok in checks.items() if not bool(ok.iloc[index])]


def _ladder(start: float, step: float, top: float) -> list:
    count = int(round((top - start) / step)) + 1
    return [round(start + i * step, 10) for i in range(count)]


def deepen(arrays: dict, events: list, group: str, kinds: tuple) -> list:
    """Build forward in time. An event of one of `kinds` that fails a condition is rebuilt on the same draws: a volume miss
    raises the event-day volume multiple, a miss the kind raises on (RAISE) deepens its own severity, a miss in
    DROP_AT_ONCE drops it. Events start from their own values (a twin from its SYN_VOL event's final ones). Still failing at
    the top of a ladder, or hit by a drop-at-once condition: ordinary walk, kind 'dropped', with the failing conditions listed."""
    d = DESIGN_V2
    m_ladder = _ladder(d["event_volume_multiple"], d["volume_multiple_step"], d["volume_multiple_max"])
    final = []
    for ev in events:
        ev = dict(ev)
        if ev["kind"] in kinds:
            kind = ev["kind"]
            key, start_key, step_key, top_key = KINDS_DEEPENED[kind]
            ladder = _ladder(d[start_key], d[step_key], d[top_key])
            signal = _span(kind, ev["start_index"])[2]
            i_s = ladder.index(ev.get(key, d[start_key]))
            i_m = m_ladder.index(ev.get("mult", d["event_volume_multiple"]))
            while True:
                ev[key], ev["mult"] = ladder[i_s], m_ladder[i_m]
                failing = _failing(build_series(arrays, group, final + [ev], signal + 1), signal, kind)
                if not failing:
                    break
                progressed = False
                if not any(name in DROP_AT_ONCE[kind] for name in failing):
                    if "volume" in failing and i_m < len(m_ladder) - 1:
                        i_m += 1
                        progressed = True
                    if any(name in RAISE[kind] for name in failing) and i_s < len(ladder) - 1:
                        i_s += 1
                        progressed = True
                if not progressed:
                    ev["failing"] = failing
                    ev["dropped_kind"] = kind
                    ev["kind"] = "dropped"
                    ev["tried_up_to"] = {key: ev.pop(key), "mult": ev.pop("mult")}
                    break
        final.append(ev)
    return final


def _describe(events: list, dates: list, frame: pd.DataFrame) -> list:
    d = DESIGN_V2
    flags = answer.signal_days(frame, d["N"], d["W"], d["k"], d["R"], d["L"])
    out = []
    for ev in events:
        first, last, day = _span(ev.get("dropped_kind", ev["kind"]), ev["start_index"])
        last = min(last, len(dates) - 1)
        row = {k: v for k, v in ev.items()}
        row.update(start_date=dates[first], end_index=last, end_date=dates[last], event_index=day, event_date=dates[day])
        if ev["kind"] in TRAP_KINDS:
            row["meets_all"] = bool(flags.iloc[first:last + 1].any())
        out.append(row)
    return out


def make(out_dir: Path, seed: int) -> dict:
    d = DESIGN_V2
    out_dir.mkdir(parents=True, exist_ok=True)
    dates = load_sessions()
    n = len(dates)
    codes: dict = {}
    frames: dict = {}
    number = 0
    vol_arrays: dict = {}
    vol_events: dict = {}
    trd_numbers: dict = {}
    for group, tag in GROUPS:
        for i in range(1, d["codes_per_group"] + 1):
            number += 1
            code = f"SYN_{tag}_{i:02d}"
            arrays = draw_arrays(seed, number, n)
            events = plan_events(group, arrays, (i - 1) * d["trend_phase_step"])
            if group == "volatile":
                events = deepen(arrays, events, group, ("planted", "decoy_donchian", "decoy_stochastic"))
            if max(_span(e.get("dropped_kind", e["kind"]), e["start_index"])[1] for e in events) >= n:
                raise RuntimeError(f"{code}: an event runs past the last session")
            frame = build_series(arrays, group, events, n)
            frames[code] = frame
            codes[code] = {"group": group, "code_number": number, "events": _describe(events, dates, frame)}
            if group == "volatile":
                vol_arrays[i], vol_events[i] = arrays, events
            if group == "trending":
                trd_numbers[i] = number
    for i in range(1, d["codes_per_group"] + 1):
        code = f"SYN_TWN_{i:02d}"
        twin_events = []
        for ev in vol_events[i]:
            ev = dict(ev)
            if ev["kind"] == "planted":
                ev["kind"] = "twin_drop"
            twin_events.append(ev)
        twin_events = deepen(vol_arrays[i], twin_events, "twin", ("twin_drop",))
        frame = build_series(vol_arrays[i], "twin", twin_events, n)
        frames[code] = frame
        codes[code] = {"group": "twin", "twin_of": f"SYN_VOL_{i:02d}", "code_number": i,
                       "events": _describe(twin_events, dates, frame)}
    for i in range(1, d["codes_per_group"] + 1):
        code = f"SYN_TTW_{i:02d}"
        arrays = draw_arrays(seed, trd_numbers[i], n)
        events = plan_events("trend_twin", arrays, (i - 1) * d["trend_phase_step"])
        if max(_span(e["kind"], e["start_index"])[1] for e in events) >= n:
            raise RuntimeError(f"{code}: an event runs past the last session")
        frame = build_series(arrays, "trend_twin", events, n)
        frames[code] = frame
        codes[code] = {"group": "trend_twin", "twin_of": f"SYN_TRD_{i:02d}", "code_number": trd_numbers[i],
                       "events": _describe(events, dates, frame)}
    for code, frame in frames.items():
        frame.insert(0, "date", dates)
        frame.to_parquet(out_dir / f"{code}.parquet", index=False)
    manifest = {"synthetic": True, "version": 2, "seed": seed, "DESIGN_V2": DESIGN_V2, "sessions": n,
                "first_session": dates[0], "last_session": dates[-1], "codes": codes}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest
