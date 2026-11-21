"""The walk-forward stage over the group (lane GV2-7a, plan steps 5 and 5b).

A port of run_sweep_entrypoint._process_walkforward (written there for one stock and a strategy object; copied here, never imported from
the baked image path), on the group. Where the strategy object is missing:
  the grid is the spec's variations (each with its stop and target rule), its combos' order the variations' order by id;
  purge: exit.max_hold, the strategy's declared time limit, largest over the variations; when the theory has none, the longest hold of
         the variations' trades in slice 0's unpulled in-sample leg (derive_purge_and_embargo's own second branch);
  embargo: the group's lookback (lookback.py), the largest over the variations;
  a slice's bars are the group's dates; its legs are run_group over the leg's dates on the whole loaded frames, so the indicators are
         warm from data_start (this plays the part of the engine's lead-in);
  the choice is the variation with the greatest in-sample total_return, the first on a tie;
  the window is read as dates both included, and handed to compute_slice_bounds, which is half open, as [window_start, the day after
         window_end).
Everything else is the engine's arithmetic: annualised run-level profits over the slices whose both profits are finite, the
out-of-sample equity chained by the running end value over init_cash, the five general figures from the chained curve and the
legs' summed trades, fees and slippage.

walkforward.json holds the body of POST /worker/runs/{id}/walkforward-result (WalkforwardResultIn); equity.parquet is the chained
curve (timestamp in unix seconds, equity). bars_observed.json (lane GV2-8b, DECISIONS #459 item 2) is {"members": {ticker: n}, "total": sum}: each
member's close bars that are not NaN in the loaded frames, which are cut from data_start to window_end, so the lead-in counts (the
data-reuse tracker records it for the stage's draw). It also lists, as `calibrated_buckets`, each gap bucket the stage computed itself
(variation, bucket, stop_sigma, target_sigma) because the variation's table lacked it (variation.TableCalibrator). A slice's fill_reach_ts and entries_masked_at_end are left out: run_group never reaches a
bar past the leg and masks nothing, and the writers read their absence as none and zero.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pandas as pd

from research_lab.theory_stage import files, lookback, variation
from strategies.base.sweep import canonical_param_json
from strategies.base.walkforward import compute_slice_bounds, derive_purge_and_embargo
from strategies.helpers.annualization import resolve_annualization


def _finite_or_none(value):
    return float(value) if value is not None and math.isfinite(value) else None


def _max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float(abs(((equity - peak) / peak).min()))


def _date(ts: int) -> str:
    return str(pd.Timestamp(ts, unit="s").date())


def run_stage(ctx, out: Path) -> dict:
    spec = ctx.spec
    window = ctx.window
    init_cash = float(spec["init_cash"])
    in_bars, oos_bars, anchoring = spec["in_sample_bars"], spec["out_of_sample_bars"], spec["anchoring"]
    index = ctx.frames["close"].index
    bar_dates = [(str(ts), int(files.unix_seconds(pd.DatetimeIndex([ts]))[0])) for ts in index]
    window_start_ts = int(files.unix_seconds(pd.DatetimeIndex([pd.Timestamp(window[0])]))[0])
    window_end_ts = int(files.unix_seconds(pd.DatetimeIndex([pd.Timestamp(window[1]) + pd.Timedelta(days=1)]))[0])
    bars_per_year = resolve_annualization(index)["factor"]

    prepared = [variation.prepare(ctx, var, window) for var in ctx.variations]
    holds = [v.rules.max_hold for v in prepared]
    declared = None if any(h is None for h in holds) else max(holds)
    embargo = lookback.group_lookback(ctx.theory, [v["picks"] for v in ctx.variations])

    provisional = compute_slice_bounds(bar_dates, window_start_ts, window_end_ts, in_bars, oos_bars, anchoring, 0, 0)
    if not provisional:
        raise ValueError("walkforward-no-out-of-sample-leg: the window %s to %s holds no slice of %d in-sample and %d out-of-sample bars"
                         % (window[0], window[1], in_bars, oos_bars))
    trade_holds = []
    if declared is None:
        first = provisional[0]
        leg = (_date(first["in_sample_start_ts"]), _date(first["in_sample_end_ts"]))
        for item in prepared:
            res = variation.run(ctx, item, leg)
            for _, t in res.trades.iterrows():
                trade_holds.append(int(index.get_loc(pd.Timestamp(t["exit_index"])) - index.get_loc(pd.Timestamp(t["entry_index"]))))
    purge, embargo = derive_purge_and_embargo(declared, trade_holds, embargo)
    slices = compute_slice_bounds(bar_dates, window_start_ts, window_end_ts, in_bars, oos_bars, anchoring, purge, embargo)
    if len(slices) < 2:
        raise ValueError("walkforward-window-too-small-after-buffer: only %d slice(s) fit once purge_bars=%d and embargo_bars=%d are applied "
                         "(in_sample_bars=%d, out_of_sample_bars=%d)" % (len(slices), purge, embargo, in_bars, oos_bars))

    emitted, parts = [], []
    used = absent = is_bars_total = oos_bars_total = 0
    is_profit_total = oos_profit_total = 0.0
    running_end = init_cash
    trades_total, fees_total, slippage_total = 0, 0.0, 0.0
    for s in slices:
        is_leg = (_date(s["in_sample_start_ts"]), _date(s["in_sample_end_ts"]))
        oos_leg = (_date(s["out_of_sample_start_ts"]), _date(s["out_of_sample_end_ts"]))
        best, best_return, best_res = None, None, None
        for item in prepared:
            res = variation.run(ctx, item, is_leg)
            candidate = res.total_return
            if candidate is not None and math.isfinite(candidate) and (best_return is None or candidate > best_return):
                best, best_return, best_res = item, candidate, res
        if best is None:
            continue
        oos_res = variation.run(ctx, best, oos_leg)
        is_profit, oos_profit = _finite_or_none(best_res.total_return), _finite_or_none(oos_res.total_return)
        trade_count = int(oos_res.num_trades + oos_res.num_open_at_end)
        if is_profit is None or oos_profit is None:
            absent += 1
        else:
            used += 1
            is_bars_total += len(best_res.equity)
            oos_bars_total += len(oos_res.equity)
            is_profit_total += is_profit
            oos_profit_total += oos_profit
        chained = oos_res.equity * (running_end / init_cash)
        parts.append(chained)
        running_end = float(chained.iloc[-1])
        trades_total += trade_count
        fees_total += float(oos_res.fees_paid)
        slippage_total += float(oos_res.slippage_paid)
        emitted.append({
            "slice_index": s["slice_index"],
            "in_sample_start_ts": s["in_sample_start_ts"], "in_sample_end_ts": s["in_sample_end_ts"],
            "out_of_sample_start_ts": s["out_of_sample_start_ts"], "out_of_sample_end_ts": s["out_of_sample_end_ts"],
            "param_config_hash": hashlib.sha256(canonical_param_json(best.picks).encode("utf-8")).hexdigest(),
            "variation_id": best.variation_id,
            "in_sample_profit": is_profit, "out_of_sample_profit": oos_profit, "trade_count": trade_count,
        })

    if is_bars_total > 0 and oos_bars_total > 0:
        run_is = (is_profit_total / is_bars_total) * bars_per_year
        run_oos = (oos_profit_total / oos_bars_total) * bars_per_year
    else:
        run_is = run_oos = None
    if not parts:
        raise ValueError("walkforward-no-out-of-sample-leg: %d slice(s) computed, none with a usable in-sample choice" % len(slices))
    stitched = pd.concat(parts)
    figures = {
        "total_return": _finite_or_none(running_end / init_cash - 1.0), "max_drawdown": _finite_or_none(_max_drawdown(stitched)),
        "total_fees_paid": _finite_or_none(fees_total), "total_slippage_cost": _finite_or_none(slippage_total),
    }
    bad = [k for k, v in figures.items() if v is None]
    if bad:
        raise ValueError("walkforward-no-out-of-sample-leg: the chained out-of-sample result has a non-finite %s" % ", ".join(bad))
    body = {
        "slices": emitted,
        "in_sample_profit": _finite_or_none(run_is), "out_of_sample_profit": _finite_or_none(run_oos),
        "wfa_boundary_buffer_bars": purge + embargo,
        "total_return": figures["total_return"], "max_drawdown": figures["max_drawdown"], "num_trades": trades_total,
        "total_fees_paid": figures["total_fees_paid"], "total_slippage_cost": figures["total_slippage_cost"],
        "slices_used": used, "slices_with_absent_profit": absent,
        "calibrated_buckets": sorted((b for item in prepared for b in item.computed), key=lambda b: (b["variation"], float(b["bucket"]))),
    }
    out.mkdir(parents=True, exist_ok=True)
    files.write_json(out / "walkforward.json", body)
    members = {str(t): int(n) for t, n in ctx.frames["close"].notna().sum().items()}
    files.write_json(out / "bars_observed.json", {"members": members, "total": sum(members.values())})
    files.write_parquet(out / "equity.parquet", files.equity_frame(stitched))
    return body
