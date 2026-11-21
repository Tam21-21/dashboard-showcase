"""python -m research_lab.theory_search.union --spec <file> --in <dir> --out <dir>  (lane GV3-FU1)

The merged trial family. AFML p. 275: the deflated Sharpe's N and V count every trial tried on the same data, so the searches of several
theories on one stock set and screen window form one trial family. This reads the screen rows of each search's own files and gives the numbers
of the whole family (family.family_summary), with no pick.

Each run's folder (named by its run id, as the worker lays them under <in>) holds trials.parquet, returns.parquet, family.json and
screen_dates.json. A trial is named "r<run_id>t<trial_id>". The returns are used as stored (float32).

--spec is a JSON file: runs (run ids), onc_cap, seed, member_set_sha256, window_start, window_end. The last three must equal every run's
family.json, or the merge is refused. --out gets union.json: run_ids sorted, member_set_sha256, window_start, window_end, onc_cap_asked,
seed, and every key family_summary returns, as it returns it.

Every refusal is a ValueError naming the runs and the field or file.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

from research_lab.theory_search import family

IDENTITY_KEYS = ("member_set_sha256", "window_start", "window_end")
FILES = ("trials.parquet", "returns.parquet", "family.json", "screen_dates.json")


def _run_id(folder: Path) -> int:
    try:
        return int(Path(folder).name)
    except ValueError:
        raise ValueError("a run folder is named by its run id, got %r" % (str(folder),)) from None


def _require(folder: Path, run: int, name: str) -> Path:
    path = Path(folder) / name
    if not path.is_file():
        raise ValueError("run %d has no %s (%s)" % (run, name, path))
    return path


def _names(runs) -> str:
    return "runs " + " and ".join(str(r) for r in runs)


def read_identity(folders) -> dict:
    """{run_id: {member_set_sha256, window_start, window_end}} from each folder's family.json; the folders' names are unique run ids."""
    out = {}
    for folder in folders:
        run = _run_id(folder)
        if run in out:
            raise ValueError("run %d is given twice" % run)
        body = json.loads(_require(folder, run, "family.json").read_text(encoding="utf-8"))
        missing = [k for k in IDENTITY_KEYS if k not in body]
        if missing:
            raise ValueError("run %d: family.json lacks %s" % (run, ", ".join(missing)))
        out[run] = {k: body[k] for k in IDENTITY_KEYS}
    return out


def union_family(folders, onc_cap: int, seed: int) -> dict:
    """family_summary over the screen trials of every folder's search, selected None. Raises ValueError (see the module doc)."""
    folders = sorted((Path(f) for f in folders), key=_run_id)
    if not folders:
        raise ValueError("a merged family needs at least one run")
    identity = read_identity(folders)
    runs = sorted(identity)
    for key in IDENTITY_KEYS:
        if len({identity[r][key] for r in runs}) != 1:
            raise ValueError("%s differ in family.json %s: %s" % (_names(runs), key, {r: identity[r][key] for r in runs}))
    dates = {}
    for folder in folders:
        run = _run_id(folder)
        dates[run] = json.loads(_require(folder, run, "screen_dates.json").read_text(encoding="utf-8"))
    if len({tuple(d) for d in dates.values()}) != 1:
        raise ValueError("%s differ in screen_dates.json: lengths %s" % (_names(runs), {r: len(d) for r, d in dates.items()}))
    index = pd.DatetimeIndex(pd.to_datetime(dates[runs[0]], utc=True))
    n_dates = len(index)
    blocks, names, num_trades = [], [], []
    for folder in folders:
        run = _run_id(folder)
        trials_path = _require(folder, run, "trials.parquet")
        returns_path = _require(folder, run, "returns.parquet")
        trials = pq.read_table(trials_path, columns=["trial_id", "window", "num_trades"])
        trials = trials.filter(pc.equal(trials["window"], "screen"))
        if trials.num_rows == 0:
            raise ValueError("run %d: trials.parquet holds no screen row" % run)
        returns = pq.read_table(returns_path, columns=["trial_id", "window", "bar_returns"])
        returns = returns.filter(pc.equal(returns["window"], "screen"))
        trial_ids = trials["trial_id"].to_pylist()
        position = {t: i for i, t in enumerate(returns["trial_id"].to_pylist())}
        if sorted(trial_ids) != sorted(position):
            raise ValueError("run %d: trials.parquet and returns.parquet hold different screen trials" % run)
        column = returns["bar_returns"].combine_chunks()
        lengths = np.diff(column.offsets.to_numpy())
        in_order = lengths[[position[t] for t in trial_ids]]
        wrong = np.flatnonzero(in_order != n_dates)
        if len(wrong):
            raise ValueError("run %d: trial %d has %d bar returns, screen_dates.json has %d dates" % (run, trial_ids[wrong[0]], in_order[wrong[0]], n_dates))
        flat = column.flatten().to_numpy(zero_copy_only=False).astype(np.float32, copy=False)
        rows = flat.reshape(len(lengths), n_dates)
        blocks.append(rows[[position[t] for t in trial_ids]])
        names.extend("r%dt%d" % (run, t) for t in trial_ids)
        num_trades.extend(trials["num_trades"].to_pylist())
    array = np.empty((n_dates, len(names)), dtype=np.float32, order="F")
    start = 0
    for block in blocks:
        array[:, start:start + block.shape[0]] = block.T
        start += block.shape[0]
    del blocks
    merged = pd.DataFrame(array, index=index, columns=names, copy=False)
    frame = pd.DataFrame({"trial_id": names, "num_trades": num_trades})
    return family.family_summary(frame, merged, onc_cap=int(onc_cap), seed=int(seed), selected=None)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="research_lab.theory_search.union")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--in", dest="inp", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    lacking = [k for k in ("runs",) + IDENTITY_KEYS + ("onc_cap", "seed") if k not in spec]
    if lacking:
        raise ValueError("the spec lacks %s" % ", ".join(lacking))
    runs = sorted(int(r) for r in spec["runs"])
    folders = [args.inp / str(r) for r in runs]
    identity = read_identity(folders)
    for key in IDENTITY_KEYS:
        wrong = [r for r in runs if identity[r][key] != spec[key]]
        if wrong:
            raise ValueError("the spec's %s %r differs from %s in family.json" % (key, spec[key], _names(wrong)))
    summary = union_family(folders, spec["onc_cap"], spec["seed"])
    body = {"run_ids": runs, **{k: spec[k] for k in IDENTITY_KEYS}, "onc_cap_asked": int(spec["onc_cap"]), "seed": int(spec["seed"]), **summary}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "union.json").write_text(json.dumps(body, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print("wrote union.json to %s: %d runs, %d trials, K %s, V %s, reason %s" % (args.out, len(runs), summary["n_trials"], summary["k_effective"], summary["v"], summary["reason"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
