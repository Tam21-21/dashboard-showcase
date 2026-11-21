"""The optimise stage: every variation over the group, written as the rows and files the judge reads (lane GV2-7a, plan step 3).

Files in the output folder (v is the variation id, L the group label):
  cells.json, funnel.json              one row per variation, research_run_cells' and research_run_funnel's columns (row_scope "cell")
  replay_equity_v{v}_{L}.parquet       timestamp (unix seconds) and equity of the group
  replay_trades_v{v}_{L}.parquet       vbt_columns.CANONICAL_TRADE_COLUMNS then ticker; `column` is the stock's index in the group
  replay_buyhold_equity_v{v}_{L}.parquet   the buy-and-hold arm (buyhold.py)
  replay_random_arm_v{v}_{L}.parquet   the random-entry arm judge check 9 grades: one row, k, m, p_value, original_statistic, seed
                                       (lane GV3-J; none for a variation with no trade, its payload then carries replay.random_arm_error)
  replay_mcpt_v{v}_{L}.parquet         check 7's re-run on shuffled price bars: one row, k, m, p_value, original_performance, seed, statistic,
                                       dropped_dates (lane GV3-J7; none when the re-run cannot be made, the payload then carries replay.mcpt_error)
  full_metrics_v{v}.json               the payload the judge admits: full metrics, provenance and the replay object
  variations.json                      variation id to picks
  cscv_blocks.parquet                  variation_id, ticker, block_index, count, sum, sumsq, bars_dropped (check 5)
  annualization.json                   what research_runs.annualization_* need: the Sharpe is annualised by sqrt(252)

How a cell is read off the group's result (GroupResult): total_return, sharpe_ratio (annualised, NaN written as null), num_trades
(closed plus open trades, as sweep_portfolio.py counts), total_fees_paid,
total_slippage_cost; max_drawdown as a positive fraction; win_rate and profit_factor over the closed trades (null when undefined);
skew and kurtosis of the bar returns (pandas, excess kurtosis).
Two more keys on every cell (lane GV2-8b, DECISIONS #459 item 1), which complete_sweep_chunk writes to research_run_variations: `lookback_bars`,
the theory's lookback at the variation's picks, and `used_degrees_of_freedom`, what judge check 1 reads (lookback.theory_degrees_of_freedom).
Funnel: bars_total is the window's bars; bars_after_warmup the window bars where every operand the conditions read is known for
every stock that has a price; raw_entries the entry signals inside the window; entries_after_dedup the buy orders; raw_exits the
sell orders (exits are rules, there is no exit signal); fills all orders; total_trades closed plus open; exits_by_signal the closed
ones; exits_by_eod the open ones. zero_reason is strategies.helpers.funnel.classify_zero_reason.
"""
from __future__ import annotations

import hashlib
import json
import sys
from glob import glob
from pathlib import Path

import numpy as np
import pandas as pd

from research_lab.judge_arms import mcpt_bars, measure_rerun, random_arm
from research_lab.theory_stage import buyhold, files, lookback, variation
from strategies.base.vbt_columns import CANONICAL_TRADE_COLUMNS
from strategies.helpers.funnel import classify_zero_reason

TRADING_DAYS = 252
MCPT_STATISTIC = "sharpe_after_costs"
RANDOM_ARM_M = 5000  # judgedb's frozen random_arm_replication_count (DECISIONS #421 item 1; Aronson folios 240 and 243)
BUILD_PROVENANCE_PATH = Path("/etc/build_provenance")
_EMPTY_TYPES = {"column": "int64", "trade_id": "int64", "entry_order_id": "int64", "exit_order_id": "int64", "position_id": "int64",
                "entry_index": "datetime64[ns]", "exit_index": "datetime64[ns]", "direction": "object", "status": "object", "ticker": "object"}


def cscv_block_sums(bar_returns: list, s_blocks: int) -> tuple:
    """Check 5's blocks: S equal contiguous blocks of (count, sum, sumsq); the leftover bars are dropped from the START.

    The same arithmetic as strategies/helpers/sweep_portfolio._cscv_block_sums and judge_checks.build_cscv_block_sums.
    """
    if len(bar_returns) < s_blocks:
        raise ValueError("the window has %d bars, fewer than the %d blocks of check 5" % (len(bar_returns), s_blocks))
    dropped = len(bar_returns) % s_blocks
    kept = bar_returns[dropped:]
    width = len(kept) // s_blocks
    blocks = []
    for i in range(s_blocks):
        chunk = kept[i * width:(i + 1) * width]
        blocks.append((len(chunk), float(sum(chunk)), float(sum(r * r for r in chunk))))
    return blocks, dropped


def _finite(value):
    return float(value) if value is not None and np.isfinite(value) else None


def trades_out(res, tickers: list) -> pd.DataFrame:
    trades = res.trades.copy()
    columns = list(CANONICAL_TRADE_COLUMNS) + ["ticker"]
    if len(trades) == 0:
        return pd.DataFrame({c: pd.Series(dtype=_EMPTY_TYPES.get(c, "float64")) for c in columns})
    position = {t: i for i, t in enumerate(tickers)}
    trades["column"] = trades["ticker"].map(position).astype("int64")
    return trades[columns].reset_index(drop=True)


def cell_row(label: str, vid: int, res) -> dict:
    closed = res.trades[res.trades["status"].str.lower() == "closed"] if len(res.trades) else res.trades
    pnl = closed["pnl"].astype(float) if len(closed) else pd.Series(dtype=float)
    gross_loss = float(-pnl[pnl < 0].sum())
    return {
        "variation_id": vid, "ticker": label, "row_scope": "cell",
        "total_return": res.total_return, "sharpe_ratio": _finite(res.sharpe), "max_drawdown": abs(res.max_drawdown),
        "num_trades": res.num_trades + res.num_open_at_end,
        "win_rate": float((pnl > 0).sum() / len(pnl)) if len(pnl) else None,
        "total_fees_paid": res.fees_paid, "total_slippage_cost": res.slippage_paid,
        "profit_factor": float(pnl[pnl > 0].sum() / gross_loss) if len(pnl) and gross_loss > 0 else None,
        "skew": _finite(res.bar_returns.skew()), "kurtosis": _finite(res.bar_returns.kurtosis()),
    }


def funnel_row(label: str, vid: int, prepared, res, window: tuple) -> dict:
    index = prepared.entries.index
    start, end = pd.Timestamp(window[0]), pd.Timestamp(window[1])
    if index.tz is not None:
        start, end = start.tz_localize(index.tz), end.tz_localize(index.tz)
    inside = np.asarray((index >= start) & (index <= end))
    orders = res.orders
    counts = {
        "bars_total": int(len(res.equity)),
        "bars_after_warmup": int(prepared.bars_ok.to_numpy()[inside].sum()),
        "raw_entries": int(prepared.entries.to_numpy()[inside].sum()),
        "entries_after_dedup": int((orders["side"] == "buy").sum()),
        "raw_exits": int((orders["side"] == "sell").sum()),
        "fills": int(len(orders)),
        "total_trades": int(res.num_trades + res.num_open_at_end),
        "exits_by_signal": int(res.num_trades),
        "exits_by_eod": int(res.num_open_at_end),
    }
    return {"variation_id": vid, "ticker": label, "row_scope": "cell", **counts, "zero_reason": classify_zero_reason(counts)}


def provenance() -> dict:
    """The container's engine, read the way run_backtest_entrypoint._environment reads it, without the hostname (it would change the bytes)."""
    import vectorbtpro as vbt  # noqa: PLC0415 - only the research-runner images carry vbt

    site_packages = Path(vbt.__file__).resolve().parent.parent
    found = glob(str(site_packages / "vectorbtpro-*.dist-info" / "direct_url.json"))
    if not found:
        raise FileNotFoundError("no vectorbtpro-*.dist-info/direct_url.json under %s; cannot derive the vbt commit" % site_packages)
    commit = json.loads(Path(found[0]).read_text(encoding="utf-8"))["vcs_info"]["commit_id"]
    if not BUILD_PROVENANCE_PATH.is_file():
        raise FileNotFoundError("%s is missing: this stage runs inside a research-runner image" % BUILD_PROVENANCE_PATH)
    return {
        "python_version": "%d.%d.%d" % sys.version_info[:3],
        "vbt_version": "%s+%s" % (vbt.__version__, commit[:8]),
        "build_provenance": BUILD_PROVENANCE_PATH.read_text(encoding="utf-8").strip(),
        "engine": "research_lab.theory_stage",
    }


def random_arm_seed(ctx, vid: int) -> int:
    """63 bits from the stage inputs (they fit the parquet int64): the same stage inputs always give the same seed."""
    text = "%s|%s|%s|%s|%d|random_arm" % (ctx.member_set_sha256, ctx.label, ctx.window[0], ctx.window[1], vid)
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big") >> 1


def random_arm_entry(ctx, vid: int, res, trades: pd.DataFrame, label: str, out: Path) -> tuple:
    """Judge check 9's random-entry arm for one variation: returns ({"name", "sha256"}, None), or (None, message) when the arm
    cannot be computed (RandomArmError, for example no trade in the window).

    Sessions are the equity index as "%Y-%m-%d" text; opens are the loaded open frame on the same text index. A trade's weight is
    size x avg_entry_price / the group equity of the bar before the entry bar (init_cash when the entry is the first bar): the share
    of group equity put in at the entry open, held on the rows entry_index to exit_index. The last window session drops out of the
    arm when the loaded frames hold no open after it (random_arm.window_returns keeps only sessions that have a next open).
    """
    sessions = [i.strftime("%Y-%m-%d") for i in res.equity.index]
    opens = ctx.frames["open"].copy()
    opens.index = [i.strftime("%Y-%m-%d") for i in opens.index]
    tickers = list(ctx.frames["close"].columns)
    equity = res.equity.to_numpy(dtype=float)
    entry_at = res.equity.index.get_indexer(pd.DatetimeIndex(trades["entry_index"]))
    exit_at = res.equity.index.get_indexer(pd.DatetimeIndex(trades["exit_index"]))
    rows = []
    for n, (_, t) in enumerate(trades.iterrows()):
        if entry_at[n] < 0 or exit_at[n] < 0:
            raise ValueError("trade %s (%s) enters %s and exits %s, a bar the group's equity index does not hold"
                             % (t["trade_id"], t["ticker"], t["entry_index"], t["exit_index"]))
        base = float(ctx.spec["init_cash"]) if entry_at[n] == 0 else equity[entry_at[n] - 1]
        rows.append({"ticker": t["ticker"], "entry_index": int(entry_at[n]), "exit_index": int(exit_at[n]),
                     "weight": float(t["size"]) * float(t["avg_entry_price"]) / base})
    seed = random_arm_seed(ctx, vid)
    try:
        weights = random_arm.weights_from_trades(rows, sessions, tickers)
        result = random_arm.random_arm(weights, opens, ctx.window, RANDOM_ARM_M, seed)
    except random_arm.RandomArmError as err:
        return None, str(err)
    name = "replay_random_arm_v%d_%s.parquet" % (vid, label)
    random_arm.write_random_arm_artifact(result, out / name)
    return {"name": name, "sha256": files.sha256_of(out / name)}, None


def mcpt_seed(ctx, vid: int) -> int:
    """random_arm_seed's rule with "mcpt" in the last place."""
    text = "%s|%s|%s|%s|%d|mcpt" % (ctx.member_set_sha256, ctx.label, ctx.window[0], ctx.window[1], vid)
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big") >> 1


def mcpt_entry(ctx, vid: int, var: dict, res, label: str, out: Path, mcpt: dict) -> tuple:
    """Judge check 7's re-run of one variation on shuffled price bars (lane GV3-J7, #422, #451): returns ({"name", "sha256"}, None), or
    (None, message) when the re-run cannot be made or does not reproduce the candidate. Nothing is written in that case; the message is
    kept as replay.mcpt_error and the judge reads check 7 as not evaluable. Any other error propagates.

    The statistic is the Sharpe after costs of the group's run (the cell's sharpe_ratio); the re-run is measure_rerun.make_perform, which rebuilds
    the wide frames from each shuffled market and runs variation.prepare and variation.run (Masters 7.4, 7.7.3)."""
    extra = sorted(k for k in ctx.frames if k not in measure_rerun.FIELDS and k != "factor")
    if extra:
        return None, ("check 7 cannot re-run variation %d: shuffled markets carry prices and the factor only; this theory also reads %s"
                      % (vid, ", ".join(extra)))
    bars = measure_rerun.to_long(ctx)
    date_sets = {t: set(f["date"]) for t, f in bars.items()}
    union = set.union(*date_sets.values())
    common = set.intersection(*date_sets.values())
    if len(union) != len(common):
        lacking = []
        for t, dates in date_sets.items():
            gone = sorted(union - dates)
            if gone:
                lacking.append("%s: %s" % (t, ", ".join(pd.Timestamp(d).strftime("%Y-%m-%d") for d in gone)))
        return None, ("check 7 cannot re-run variation %d: %d loaded date(s) are missing for some stock (%s); the shuffle needs every stock on "
                      "the same dates (Masters 7.7.3)" % (vid, len(union) - len(common), "; ".join(lacking)))
    want = _finite(res.sharpe)
    if want is None:
        return None, "variation %d has no finite Sharpe after costs, so check 7 has no original" % vid
    try:
        result = mcpt_bars.mcpt(measure_rerun.make_perform(ctx, var, ctx.window), bars, ctx.window[0], mcpt["m"], mcpt_seed(ctx, vid),
                                statistic_name=MCPT_STATISTIC, workers=mcpt["workers"])
    except mcpt_bars.MCPTError as err:
        return None, str(err)
    if result["original"] != want:
        return None, ("check 7's re-run of variation %d does not reproduce the candidate: original %r, the candidate's %r"
                      % (vid, result["original"], want))
    name = "replay_mcpt_v%d_%s.parquet" % (vid, label)
    mcpt_bars.write_mcpt_artifact(result, out / name)
    return {"name": name, "sha256": files.sha256_of(out / name)}, None


def run_stage(ctx, out: Path) -> dict:
    from strategies.helpers.full_metrics import compute_full_metrics  # noqa: PLC0415 - needs vbt

    spec, label, window = ctx.spec, ctx.label, ctx.window
    tickers = list(ctx.frames["close"].columns)
    out.mkdir(parents=True, exist_ok=True)
    buy_hold = buyhold.buy_hold_equity(ctx.frames, window, spec["init_cash"], spec["costs"])
    env = provenance()
    cells, funnels, picks_by_id, cscv_rows = [], [], {}, []
    for var in ctx.variations:
        vid = var["id"]
        prepared = variation.prepare(ctx, var, window)
        res = variation.run(ctx, prepared, window)
        cell = cell_row(label, vid, res)
        cell["lookback_bars"] = lookback.theory_lookback(ctx.theory, var["picks"])
        cell["used_degrees_of_freedom"] = lookback.theory_degrees_of_freedom(ctx.theory, var["picks"])
        cells.append(cell)
        funnels.append(funnel_row(label, vid, prepared, res, window))
        picks_by_id[str(vid)] = var["picks"]

        equity_name = "replay_equity_v%d_%s.parquet" % (vid, label)
        trades_name = "replay_trades_v%d_%s.parquet" % (vid, label)
        buyhold_name = "replay_buyhold_equity_v%d_%s.parquet" % (vid, label)
        trades = trades_out(res, tickers)
        equity_sha = files.write_parquet(out / equity_name, files.equity_frame(res.equity))
        trades_sha = files.write_parquet(out / trades_name, trades)
        buyhold_sha = files.write_parquet(out / buyhold_name, files.equity_frame(buy_hold))
        arm_entry, arm_error = random_arm_entry(ctx, vid, res, trades, label, out)
        mcpt_result, mcpt_error = mcpt_entry(ctx, vid, var, res, label, out, spec["mcpt"])

        blocks, dropped = cscv_block_sums([float(r) for r in res.bar_returns], spec["cscv_s"])
        for i, (count, total, totalsq) in enumerate(blocks):
            cscv_rows.append({"variation_id": vid, "ticker": label, "block_index": i, "count": count, "sum": total,
                              "sumsq": totalsq, "bars_dropped": dropped})

        metrics = compute_full_metrics(
            equity=res.equity, trades=trades, max_gross_exposure=None, turnover=None,
            total_fees_paid=res.fees_paid, total_slippage_cost=res.slippage_paid)
        cell_metrics = {k: cell[k] for k in ("total_return", "sharpe_ratio", "max_drawdown", "num_trades", "win_rate", "total_fees_paid",
                                              "total_slippage_cost", "profit_factor", "skew", "kurtosis")}
        payload = dict(metrics)
        payload["provenance"] = env
        payload["replay"] = {
            "cell_metrics": cell_metrics, "expected_cell_metrics": cell_metrics, "tolerance": "exact",
            "member_dataset_sha256": ctx.member_set_sha256,
            "artifacts": {"equity": {"name": equity_name, "sha256": equity_sha}, "trades": {"name": trades_name, "sha256": trades_sha}},
            "buy_hold_error": None, "buy_hold_artifacts": {"equity": {"name": buyhold_name, "sha256": buyhold_sha}},
        }
        if arm_entry is not None:
            payload["replay"]["artifacts"]["random_arm"] = arm_entry
        else:
            payload["replay"]["random_arm_error"] = arm_error
        if mcpt_result is not None:
            payload["replay"]["artifacts"]["mcpt"] = mcpt_result
        else:
            payload["replay"]["mcpt_error"] = mcpt_error
        files.write_json(out / ("full_metrics_v%d.json" % vid), payload)

    files.write_json(out / "cells.json", cells)
    files.write_json(out / "funnel.json", funnels)
    files.write_json(out / "variations.json", picks_by_id)
    cscv = pd.DataFrame(cscv_rows).astype({"variation_id": "int64", "block_index": "int64", "count": "int64", "bars_dropped": "int64"})
    files.write_parquet(out / "cscv_blocks.parquet", cscv)
    day = pd.Timedelta(days=1)
    files.write_json(out / "annualization.json", {
        "annualization_factor": float(TRADING_DAYS), "annualization_freq": str(day), "annualization_year_freq": str(day * TRADING_DAYS)})
    return {"variations": len(cells), "cells": cells}
