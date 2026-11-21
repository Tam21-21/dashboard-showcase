"""research-runner run-backtest entrypoint (M3 brief D, arch §7, DECISIONS #20).

Baked into research-runner:m3-latest at /app/run_backtest_entrypoint.py. Owns
run-finalize: it is the ONLY process that imports vbt and touches the portfolio.
The host worker never sees a `pf` object — it reads the artifacts and meta.json
off disk after this container exits.

Flow (arch §7 "Container entrypoint owns run-finalize"):
  1. read /app/run_spec.json and import /app/strategy.py, selecting the single
     concrete strategies.base.Strategy subclass (DECISIONS #20 OD-3);
  2. call Strategy.run() ONCE (live settings); run() returns the six-slot
     RunResult, and its stats already carry total_slippage_cost — the direct
     per-fill execution shortfall (DECISIONS #66/#67), not a two-pass diff;
  3. serialise the six canonical RunResult slots (run() already extracted them);
  4. write seven artifacts to /data/runs/{run_id}/ (five parquet, two JSON);
     price.parquet was added 2026-05-21 (DECISIONS #23 P2) for the on-price overlay;
  5. author meta.json's environment section (python/vbt/build_provenance/hostname).

The run context (dataset path, settings, run_id) arrives in the mounted
run_spec.json, NOT argv, so a settings list crosses cleanly (OD-2). Strategy
source mounts separately at /app/strategy.py.

sys.path[0] is /app (the script dir), so `import strategies.base` resolves to the
baked package and the mounted strategy's `from strategies.base import ...` resolves
to the same classes.
"""
from __future__ import annotations  # lazy annotations

import dataclasses  # asdict() the resolved SizingSpec into meta.json's sizing_spec field (#376 ruling 4)
import importlib.util  # load the mounted strategy file as a module
import inspect  # discover the Strategy subclass
import json  # run_spec, stats.json, meta.json
import socket  # hostname for meta.json
import sys  # version + exit code + sys.path
import traceback  # error reporting at the process boundary
import urllib.parse  # decode the price-mount dir name back to the raw trade_time_symbol (J1 §3)
from glob import glob  # locate the vbt dist-info
from pathlib import Path  # filesystem paths

import pandas as pd  # frames + parquet I/O
import vectorbtpro as vbt  # version string + dist-info location (the only direct vbt use here)

from strategies.base import (  # the baked contract surface
    RunResult,
    SettingRow,
    Strategy,
    StrategyInput,
)
from strategies.base.sweep import canonical_param_json  # lane DG (`6.1g`): the ONE canonical combo-JSON
# writer, the same function run_sweep_entrypoint.py already calls; the backend computes nothing (never a
# second, drifting copy of this rule inside dashboard/backend, area map trap 1)
from strategies.helpers import resolve_annualization  # Phase-2 Unit 1: record the price run's annualization
from strategies.helpers import classify_zero_reason  # Phase-2 Unit 3: derive the funnel's WHY-ZERO bucket
from strategies.helpers.portfolio import LeadInStrategyInput  # lane WU (5.8c): the SizedStrategyInput
# subclass (#376 ruling 4's own resolved_sizing) plus the additive entries_not_before field this lane
# now uses on the single-run path too, mirroring run_sweep_entrypoint.py's own import (it replaces the
# bare SizedStrategyInput import this file no longer needs: every price-run construction site now uses
# LeadInStrategyInput)
from strategies.helpers.sizing import (  # lane WU (5.8c): the same lead-in clip lane SW already built,
    InsufficientLeadInError,  # named refusal when the dataset holds too few bars to anchor the lead-in
    clip_frame_with_lead_in,  # never a second, hand-rolled clip: reuses the one function every other path calls
)
from strategies.helpers.alpaca_fees import CAT_PER_SHARE, SEC_FEE_RATE, TAF_PER_SHARE  # FEE1b (#459 item 4): Alpaca's three rates, read at run time and never restated
from strategies.helpers.sizing_methods import (  # #376 ruling 4: the sizing-method registry (option a)
    SizingMethod,
    resolve as resolve_sizing_method,
)

RUN_SPEC_PATH = Path("/app/run_spec.json")  # mounted run context
STRATEGY_PATH = Path("/app/strategy.py")  # mounted DB-versioned strategy source
RUNS_ROOT = Path("/data/runs")  # the run output dir mounts at /data/runs/{run_id}
BUILD_PROVENANCE_PATH = Path("/etc/build_provenance")  # baked at image build


def _log(msg: str) -> None:
    """Stdout line, flushed so the worker streams it as a job log (matches fetch.py)."""
    print(msg, flush=True)


class StaleDatasetError(ValueError):
    """Lane D1: an EODHD daily dataset fetched before fetch.py wrote the per-day `Factor` column. Sizing
    would silently count shares in adjusted units for it, so the run refuses and names the file."""


def load_price_dataset(dataset_path: str) -> pd.DataFrame:
    """Read one price dataset parquet (title-case OHLCV, DatetimeIndex) for a run, refusing an EODHD
    daily file that lacks `Factor` (lane D1, real-share sizing). The source and timeframe are read off
    the container path fetch.py writes, /data/<source>/<symbol>/<timeframe>/<start>_<end>.parquet.
    Other sources (Alpaca, Binance) and timeframes carry no as-traded price, so no Factor is expected."""
    frame = pd.read_parquet(dataset_path)
    parts = Path(dataset_path).parts
    if len(parts) >= 5 and parts[1] == "data" and parts[2] == "eodhd" and parts[4] == "1d" \
            and "Factor" not in frame.columns:
        raise StaleDatasetError(
            f"dataset {dataset_path} is an eodhd daily file without the Factor column (fetched before "
            "real-share sizing); re-fetch this dataset"
        )
    return frame


def _load_strategy_class() -> type[Strategy]:
    """Import the mounted strategy file and return its single concrete Strategy subclass."""
    spec = importlib.util.spec_from_file_location("user_strategy", STRATEGY_PATH)  # module from a path
    if spec is None or spec.loader is None:  # malformed path / loader
        raise ImportError(f"cannot load a module spec from {STRATEGY_PATH}")
    module = importlib.util.module_from_spec(spec)  # fresh module object
    spec.loader.exec_module(module)  # run the file (imports strategies.base, defines the class)

    # Only classes DEFINED in the mounted file (module == 'user_strategy'), concrete, Strategy-derived.
    candidates = [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, Strategy)
        and obj is not Strategy
        and obj.__module__ == "user_strategy"
        and not inspect.isabstract(obj)
    ]
    if len(candidates) != 1:  # zero or many → ambiguous; fail loudly (OD-3)
        names = [c.__name__ for c in candidates]
        raise ValueError(
            f"expected exactly one concrete Strategy subclass in {STRATEGY_PATH}, found {len(candidates)}: {names}"
        )
    return candidates[0]


def _vbt_version() -> str:
    """`<vectorbtpro.__version__>+<commit_id[:8]>`; commit from the dist-info direct_url.json (arch §7)."""
    site_packages = Path(vbt.__file__).resolve().parent.parent  # .../site-packages/vectorbtpro/__init__.py → site-packages
    matches = glob(str(site_packages / "vectorbtpro-*.dist-info" / "direct_url.json"))  # the VCS install record
    if not matches:  # no direct_url.json means this was not a git+ install — cannot build the id
        raise FileNotFoundError(
            f"no vectorbtpro-*.dist-info/direct_url.json under {site_packages}; cannot derive vbt commit"
        )
    info = json.loads(Path(matches[0]).read_text(encoding="utf-8"))  # parse the install record
    commit_id = info["vcs_info"]["commit_id"]  # the pinned commit SHA (KeyError if shape changed → fail loud)
    return f"{vbt.__version__}+{commit_id[:8]}"  # e.g. "2026.4.7+b1727ac6"


def _environment(sizing_method: "SizingMethod | None" = None) -> dict[str, str]:
    """The meta.json environment fields, authored here (OD-7: container is the authority).

    Always carries the four original fields plus ``cost_model`` (#376 ruling 4): Alpaca's
    three fee rates (FEE1b), read from ``alpaca_fees.py`` at call time and never restated, so a
    drift between that module and the settings-layout's display-only ``fees.alpaca.*`` rows shows up
    as a distinct value across runs (the ``cost_model_distinct_values`` monitor) rather than staying
    invisible. ``cost_model`` needs no loaded strategy, so it is always present.

    ``sizing_method`` (a resolved ``SizingMethod``, or ``None``) additionally carries
    ``sizing_method``/``sizing_spec`` when given: the registry id a strategy declared and the JSON of
    its resolved SizingSpec's five fields, keys sorted, exactly as the container resolved them. ``None``
    is the honest absence for a caller that never loaded a strategy at all (compute_metrics_entrypoint's
    'single' DERIVE mode re-extracts stored artifacts and loads no strategy), never a placeholder value.
    """
    cost_model = {
        "fixed": "alpaca",
        "sec_fee_rate": float(SEC_FEE_RATE),
        "taf_per_share": float(TAF_PER_SHARE),
        "cat_per_share": float(CAT_PER_SHARE),
    }
    env: dict[str, object] = {
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",  # "3.12.13"
        "vbt_version": _vbt_version(),  # version + commit
        "build_provenance": BUILD_PROVENANCE_PATH.read_text(encoding="utf-8").strip(),  # "git:...+vbt:..."
        "hostname": socket.gethostname(),  # container hostname
        "cost_model": json.dumps(cost_model, sort_keys=True),  # JSON document (settings_snapshot's own idiom)
    }
    if sizing_method is not None:  # a strategy was loaded and its declared method resolved (never a guess)
        env["sizing_method"] = sizing_method.id
        env["sizing_spec"] = json.dumps(dataclasses.asdict(sizing_method.spec), sort_keys=True)
    return env


def _compute_variation(strat: Strategy) -> "dict | None":
    """Lane DG (`6.1g`): the single run's own recorded combo and both Pardo figures, or ``None``
    when the strategy has not opted in to the contract at all.

    Mirrors ``run_sweep_entrypoint.py``'s own per-cell idiom (its ``lookback_bars``/
    ``used_degrees_of_freedom`` try/except block): ``None`` on the named ``NotImplementedError``
    for EACH field independently, never a substituted number. The whole return value is ``None``
    only when the strategy declares no ``single_run_params`` at all -- a strategy predating this
    lane's contract, an event or bespoke run, or the generated skeleton (row `3.2c`, not this
    lane) -- so the caller writes NO ``variation`` key into ``meta.json`` and the run proceeds
    exactly as it did before this lane, per criterion 2's own "byte-unchanged run" requirement.
    """
    try:
        combo = strat.single_run_params()  # the strategy's own honest source, never introspected
    except NotImplementedError:
        return None  # opt-in contract method not declared: no combo, no variation, no row
    try:
        lookback_bars_value = strat.lookback_bars(combo)
    except NotImplementedError:
        lookback_bars_value = None  # declared the combo but not the lookback: an honest NULL
    try:
        used_degrees_of_freedom_value = strat.used_degrees_of_freedom(combo)
    except NotImplementedError:
        used_degrees_of_freedom_value = None  # declared the combo but not Udf: an honest NULL
    return {
        "param_json": canonical_param_json(combo),  # the ONE canonical JSON writer (never re-derived here)
        "lookback_bars": lookback_bars_value,
        "used_degrees_of_freedom": used_degrees_of_freedom_value,
    }


def _write_equity(equity: "pd.Series", out_dir: Path) -> None:
    """equity.parquet: timestamp (UNIX seconds UTC int) + equity (real), one row per bar (§3.8.3)."""
    frame = pd.DataFrame(
        {
            "timestamp": equity.index.asi8 // 10**9,  # tz-aware DatetimeIndex ns → seconds
            "equity": equity.to_numpy(),  # account value per bar
        }
    )
    frame.to_parquet(out_dir / "equity.parquet", index=False)  # no index column; timestamp is explicit


def _write_indicators(indicators: dict[str, "pd.Series"], out_dir: Path) -> None:
    """indicators.parquet: one column per indicator key, DatetimeIndex preserved (§3.8.4)."""
    frame = pd.DataFrame(indicators)  # aligns the series on their shared DatetimeIndex
    frame.to_parquet(out_dir / "indicators.parquet")  # keep the index (datetime64) for the export layer


def _write_price(prices: "pd.DataFrame", out_dir: Path) -> None:
    """price.parquet: timestamp (UNIX seconds UTC int) + OHLCV (real), one row per bar (§3.8.7).

    Added 2026-05-21 (DECISIONS #23, on-price overlay P2). RunExplorer draws the moving-average
    lines on the price chart, but no other artifact carries OHLC. This re-serialises the
    StrategyInput prices (title-case columns from the M2 fetch) into the explicit-timestamp shape
    equity.parquet uses, so the export layer reads price the same way it reads equity. A close
    column in indicators.parquet was rejected: the bundle transform turns every indicators column
    into a toggleable indicator series, which price is not.
    """
    frame = pd.DataFrame(
        {
            "timestamp": prices.index.asi8 // 10**9,  # tz-aware DatetimeIndex ns → seconds
            "open": prices["Open"].to_numpy(),
            "high": prices["High"].to_numpy(),
            "low": prices["Low"].to_numpy(),
            "close": prices["Close"].to_numpy(),
            "volume": prices["Volume"].to_numpy(),
        }
    )
    frame.to_parquet(out_dir / "price.parquet", index=False)  # explicit timestamp; matches equity


def read_run_slices(channel_root: Path, run_id) -> dict[str, "pd.DataFrame"]:
    """Read exactly ``{run_id}.parquet`` from each subdir of a mounted channel root (J1 §3).

    The cross-run guard: a mounted channel dir (e.g. ``/data/events/{event_type}``) accumulates
    EVERY run's ``{run_id}.parquet`` (run_backtest.py:217,219,230), so we read THIS run's file by
    name, never a glob that would load a foreign run's rows. Generic by design: the SAME reader
    serves ``/data/events`` and ``/data/price`` (and a future channel), with no per-channel
    whitelist — it returns ``{subdir_name: frame}`` for whatever subdirs are mounted.
    """
    out: dict[str, "pd.DataFrame"] = {}  # subdir name -> this run's slice
    if not channel_root.is_dir():  # the channel was not mounted for this run (e.g. no price slices) -> empty
        return out
    for sub in sorted(channel_root.iterdir()):  # one mounted subdir at a time (deterministic order)
        if not sub.is_dir():  # skip stray files; only per-key subdirs carry slices
            continue
        parquet = sub / f"{run_id}.parquet"  # THIS run's slice only (read-by-run_id, never a glob)
        if parquet.exists():  # a subdir may legitimately lack this run's file
            out[sub.name] = pd.read_parquet(parquet)  # the materialized slice / OHLCV frame
    return out


def build_event_input(spec: dict, run_id, data_root: Path = Path("/data")) -> StrategyInput:
    """Build the event-run ``StrategyInput`` from the mounted event + price slices (J1 §3).

    Reads the event-type slices (``/data/events/*``) into ``events`` keyed by event type, and the
    per-symbol price slices (``/data/price/*``) into ``event_prices`` keyed by the RAW
    ``trade_time_symbol`` (decoding each dir name from the §7 filesystem-safe encoding, the encode
    side being ``urllib.parse.quote(symbol, safe="")``). The base ``prices`` is empty (an event run
    has N per-symbol frames, not one); ``window`` carries the dispatch window for the regimes slot
    (BN-J1-2). ``data_root`` is parameterized so the probe can point it at a synthetic layout.
    """
    events = read_run_slices(data_root / "events", run_id)  # {event_type: materialized slice}
    price_dirs = read_run_slices(data_root / "price", run_id)  # {encoded_symbol: OHLCV frame}
    event_prices = {urllib.parse.unquote(name): frame for name, frame in price_dirs.items()}  # decode -> raw symbol
    window = (int(spec["knowledge_from"]), int(spec["knowledge_to"]))  # the run window (BN-J1-2)
    # M3.75 FUND-3 (§8.4): the fundamentals channel, read through the SAME generic read_run_slices.
    # The FUND-2 worker step stages the single fact table at /data/fundamentals/facts/{run_id}.parquet,
    # so the generic reader returns {'facts': fact_table} (or {} when no fundamentals were mounted —
    # a price/event-only run). Unwrap the one 'facts' subdir to the single frame StrategyInput carries;
    # a future second fundamentals source would add a second subdir and the consumer would concat (an
    # additive change, not a rework). The container channel reader stays generic (§8.4, §15).
    fund_slices = read_run_slices(data_root / "fundamentals", run_id)  # {'facts': fact_table} or {}
    fundamentals = fund_slices.get("facts", pd.DataFrame())  # the single fact-table frame (or empty)
    return StrategyInput(prices=pd.DataFrame(), events=events, features={}, event_prices=event_prices,
                         window=window, fundamentals=fundamentals)


def main() -> int:
    """Run the backtest once, write the artifacts, return a process exit code."""
    try:
        spec = json.loads(RUN_SPEC_PATH.read_text(encoding="utf-8"))  # {dataset_path?, settings, run_id, ...}
        run_id = spec["run_id"]  # output dir name
        dataset_path = spec.get("dataset_path")  # None / absent for an event run (brief I); a path for a price run
        settings = [SettingRow(**row) for row in spec["settings"]]  # typed settings rows

        out_dir = RUNS_ROOT / str(run_id)  # the :rw-mounted output dir (pre-created by the worker)
        _log(f"[entrypoint] run_id={run_id} dataset={dataset_path} settings_rows={len(settings)}")

        strategy_cls = _load_strategy_class()  # the single Strategy subclass in the mounted file
        _log(f"[entrypoint] strategy={strategy_cls.__name__}")

        # #376 ruling 4: resolve the strategy's declared sizing method through the registry BEFORE
        # any bar is read. An id the registry does not carry raises UnknownSizingMethodError here,
        # at the gate, and the outer except below turns it into the same clean machine-readable
        # error line every other failure gets -- never a silent fall-back to the registry's default.
        sizing_method = resolve_sizing_method(strategy_cls.sizing_method)
        _log(f"[entrypoint] sizing_method={sizing_method.id}")

        # Branch BEFORE the single-frame read: an event run has no base price dataset (dataset_path
        # is None/absent), so reading it would crash (J1 §3). The event path builds StrategyInput from
        # the mounted event + price slices; the price path is byte-unchanged from M3.
        is_event = dataset_path is None  # an event run carries no base price dataset
        if is_event:  # event run (J1): build from the mounted slices
            data = build_event_input(spec, run_id)  # events + per-symbol price + window, base prices empty
            prices = None  # no base price frame -> guards _write_price below (the prices["Close"] guard, §3)
            _log("[entrypoint] kind=event_run")  # event-run marker
        else:  # price run (M3): the original single-dataset path
            prices = load_price_dataset(dataset_path)  # OHLCV frame (+ Factor on eodhd daily, lane D1), DatetimeIndex
            # #376 ruling 4 round 2 (E-R1-B1): bind the already-resolved spec so long_only_signal_result
            # (the strategy's own run() calls it with no sizing= at all) sizes with the SAME method
            # meta.json records, instead of silently reading its own bare default.
            #
            # Lane WU (5.8c, `DECISIONS.md` #383 decision 2, criterion 2 of the set 12 plan section 6):
            # this line used to bind a bare SizedStrategyInput here because "a single price run always
            # reads the WHOLE dataset, so there is no window boundary to lead into" (lane SW's own
            # comment, now corrected). That reasoning missed that the SIZING ESTIMATOR still needs
            # estimator_window bars of warm-up before its FIRST usable entry, whole-dataset read or not
            # (measured on the real strategy-run set: r1/r5 aborted with UndefinedVolatilityError at the
            # very first crossing because nothing masked an entry inside the estimator's own warmup
            # region). The fix keeps the dataset UNCHANGED (bars_total stays the dataset length, so
            # judge_checks.py's lead-in subtraction is exact) and only marks WHERE entries may start:
            # the bar at position estimator_window is used as clip_frame_with_lead_in's own
            # window_start_ts, so lead_in_start_pos always resolves to 0 (estimator_window bars before
            # position estimator_window is position 0) and the whole frame is returned unclipped -- the
            # ONLY effect is the returned entries_not_before boundary, which mask_entries_before then
            # applies inside long_only_signal_result exactly like every other lead-in path.
            estimator_window = sizing_method.spec.estimator_window  # this run's own resolved warm-up length
            if len(prices.index) <= estimator_window:  # not even one bar exists to anchor entries_not_before on
                raise InsufficientLeadInError(
                    f"single_run: only {len(prices.index)} bars are available in this dataset, at or "
                    f"below the {estimator_window}-bar lead-in the estimator needs (a bar at position "
                    f"{estimator_window} is required to anchor the lead-in boundary); "
                    "reason=insufficient_lead_in"
                )
            # The bar's own timestamp, as whole UTC seconds (clip_frame_with_lead_in's own contract),
            # never a fractional float: daily/hourly bars land on whole seconds, so int() never truncates
            # real precision away.
            lead_in_boundary_ts = int(prices.index[estimator_window].timestamp())
            prices, entries_not_before = clip_frame_with_lead_in(
                prices, lead_in_boundary_ts, None, estimator_window, "single_run",
                lead_in_bars=estimator_window,  # P2e1: this anchor is bar-counted on every bar size
            )
            data = LeadInStrategyInput(
                prices=prices, resolved_sizing=sizing_method.spec, entries_not_before=entries_not_before,
            )

        # --- the single run: live settings ---
        # No shadow pass: total_slippage_cost is the direct per-fill execution shortfall that run()
        # folded into stats (DECISIONS #66/#67), not a main-vs-shadow terminal-equity diff.
        _log("[entrypoint] phase=main")  # locked run-phase marker (probe D-2.1)
        strat = strategy_cls()  # one instance for both the V7 hook and the run (A3.2)
        # Lane DG (`6.1g`): the strategy's own single-run combo, read right after instantiation and
        # before run() so its presence never depends on how run() itself executes. None for a
        # strategy that has not declared the opt-in contract method at all (criterion 2).
        variation = _compute_variation(strat)
        # V7 (A3): the input-validation gate — inputs assembled, run not started. The default base
        # hook is a no-op; a strategy that overrides it may raise StrategyInputError, which the
        # except below turns into the clean machine-readable error line (not an uncaught traceback).
        strat.validate_input(data, settings)
        res: RunResult = strat.run(data, settings)  # full RunResult, all six slots + the eight stats

        # stats.json = the run's canonical metrics (total_slippage_cost is among them, §3.5 / §3.8.5).
        # Phase-2 Unit 7 (Wave-2 minor): run() merges the Unit-3 signal-funnel counts into RunResult.stats
        # under a `funnel_` namespace (portfolio.py:85) as the only transport out of run(); they are
        # recorded below in meta["funnel"] -> the research_run_funnel fact table. Strip them from this
        # copy so the funnel has ONE store — never duplicated into stats.json, nor into research_run_stats
        # (whose write projects a fixed 8-column set, so it ignores them regardless). res.stats stays
        # intact, so the funnel extraction below still reads the counts from it.
        stats = {k: v for k, v in res.stats.items() if not k.startswith("funnel_")}  # drop the funnel_ transport keys
        stats["num_trades"] = int(stats["num_trades"])  # schema is INTEGER; build_run_result stored a float

        # --- write the seven artifacts directly into the mounted :rw dir (no copy step) ---
        _write_equity(res.equity, out_dir)  # equity.parquet
        res.drawdowns.to_parquet(out_dir / "drawdowns.parquet", index=False)  # 9 canonical cols
        res.trades.to_parquet(out_dir / "trades.parquet", index=False)  # 20 canonical cols
        # indicators.parquet ONLY when there are indicators. An empty dict (every event run — §8.6
        # event pipeline returns indicators={} — and any indicator-less price run) would write an
        # index-less empty frame whose RangeIndex the export datetime read chokes on (the run-result
        # black screen). Mirrors the price.parquet guard below: an absent optional artifact is the
        # export layer's {} case, not an error.
        if res.indicators:  # skip the write entirely for an empty indicator set
            _write_indicators(res.indicators, out_dir)  # indicators.parquet (datetime64 index)
        if not is_event:  # price.parquet only for a price run; an event run has no single base OHLCV frame (§3 guard)
            _write_price(prices, out_dir)  # price.parquet (OHLCV, for the on-price overlay; §3.8.7)
        (out_dir / "stats.json").write_text(json.dumps(stats), encoding="utf-8")  # flat stats (+ coverage_* for event runs)
        # meta.json carries the environment AND the regimes slot (J1 §12): build_run_bundle reads
        # bundle["regimes"] from here so the rule-based band renders. A price run's regimes is {} (harmless).
        meta = {"environment": _environment(sizing_method), "regimes": res.regimes}  # room for more run metadata
        if variation is not None:  # lane DG (`6.1g`): the strategy declared its own single-run combo
            meta["variation"] = variation  # written beside funnel; an undeclared combo writes no key at all
        if is_event:  # an event run also records its universe window + event types so the report renders a
            meta["event_window"] = [int(spec["knowledge_from"]), int(spec["knowledge_to"])]  # universe block,
            meta["event_types"] = list(spec.get("event_types", []))  # not a price-dataset block (BN-I, §12)
            # Phase-2 Unit 7: an event run records the SAME three annualization columns a price run does,
            # so M5 cross-run Sharpe comparison reads the column for EVERY run (they were NULL for event
            # runs — the Unit-1 carve-out). The event path annualizes on the fixed daily 252 convention
            # (event_pipeline EVENT_ANNUALIZATION_FREQ/_YEAR_FREQ) that vbt actually applied to this run's
            # Sharpe; event_annualization() returns exactly those values, normalized to the price path's
            # recording form — uniform columns by construction (not an index re-derivation, which could
            # record a different factor than the one applied).
            from strategies.base.event_pipeline import event_annualization  # the event path's fixed annualization
            meta["annualization"] = event_annualization()  # {factor: 252.0, freq, year_freq}, column-keyed
        else:  # Phase-2 Unit 1: a price run records the index-derived Sharpe annualization it used (A2b-001).
            # The same pure resolve the strategy's from_signals used (prices.index == data.close.index), so the
            # recorded factor/freq/year_freq are exactly what annualized this run's Sharpe — auditable per run.
            ann = resolve_annualization(prices.index)  # {factor: float, freq: Timedelta, year_freq: Timedelta}
            # Keys = the research_runs column names so complete_run writes them with the uniform .get(c)
            # pattern (the env-column pattern). Timedeltas -> str() for JSON; pd.Timedelta(s) parses them
            # back if a future reader needs the duration (the columns are audit/record values, not re-fed).
            meta["annualization"] = {
                "annualization_factor": ann["factor"],
                "annualization_freq": str(ann["freq"]),
                "annualization_year_freq": str(ann["year_freq"]),
            }
            # Phase-2 Unit 3 (audit concern #1): assemble the run's signal-funnel record. run() merged the
            # numeric counts into stats under a `funnel_` namespace (the only transport out of run()); here
            # we strip the prefix, derive the WHY-ZERO bucket with the same shared classifier the probe
            # tests, and record the funnel keyed by the research_run_funnel column names (the
            # meta["annualization"] pattern). A price run built through long_only_signal_result always
            # carries the funnel keys; a bespoke strategy that bypasses the shared helper carries none, so
            # the guard leaves meta["funnel"] absent (an honest opt-out, not a silent gap).
            funnel_counts = {k[len("funnel_"):]: int(v) for k, v in res.stats.items() if k.startswith("funnel_")}
            if funnel_counts:  # present for every price run built the sanctioned way
                # P2g: the lead-in bars this run's clip used, counted on the frame it ran on: the bars before
                # entries_not_before (estimator_window of them on the anchored single run). 0 when nothing led in.
                lead_in_bars = 0 if entries_not_before is None else int((prices.index < entries_not_before).sum())
                meta["funnel"] = {**funnel_counts, "zero_reason": classify_zero_reason(funnel_counts),
                                  "lead_in_bars": lead_in_bars}
        (out_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")  # meta_path target

        _log(f"[entrypoint] wrote artifacts to {out_dir}")
        # Final JSON line: a clear success signal in the log; the worker reads the files off disk.
        print(json.dumps({"run_id": run_id, "num_trades": stats["num_trades"],
                           "total_slippage_cost": stats["total_slippage_cost"]}), flush=True)
        return 0
    except Exception as e:  # noqa: BLE001 — process boundary, mirrors fetch.py: emit a clean error line + fail
        traceback.print_exc(file=sys.stderr)  # full trace to stderr for debugging
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)  # machine-readable error on stdout
        return 1


if __name__ == "__main__":
    sys.exit(main())  # propagate the exit code to docker (worker keys off rc)
