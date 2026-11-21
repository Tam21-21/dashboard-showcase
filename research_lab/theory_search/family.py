"""The numbers a trial family gives the judge: K, the effective number of trials, and V, the variance of the clustered trials.

Lane GV2-5b, plan step 7. A family is every strategy the search tried on one window (a theory, a stock group, the screen window).

Rules (the plan):
  trials with zero variance (no trade, a flat equity) are left out of the clustering and counted;
  fewer than 2 trials clearing the 30-trade bar, or too few trials with variance to cluster: no ONC, and K, the number in the
  clustering, the cap, whether the cap bound, and V are null, with the reason "too-few-trials". Too few to cluster means fewer
  than 3 here: the silhouette needs 2 to N - 1 clusters (a stated step beyond the plan's "fewer than 2" for the case of exactly 2);
  otherwise ONC runs on the correlation of the trials' bar returns with its top call's maxNumClusters = min(N - 1, cap), and the
  reason is null. cap_bound is true when K reached the cap.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research_lab.theory_search import onc

REASON_TOO_FEW = "too-few-trials"
MIN_TRADES = 30  # the coarse bar of DECISIONS #440


def family_summary(trials: pd.DataFrame, returns: pd.DataFrame, onc_cap: int, seed: int, selected, min_trades: int = MIN_TRADES, deadline=None) -> dict:
    """`trials`: columns trial_id and num_trades. `returns`: bar returns, a DatetimeIndex, one column per trial_id.

    `selected` is the trial_id of the pick; None when there is no pick, and then the frequency is read from the returns' own
    dates (every trial of the family shares them).
    """
    if "trial_id" not in trials.columns or "num_trades" not in trials.columns:
        raise ValueError("trials needs the columns trial_id and num_trades")
    if sorted(trials["trial_id"]) != sorted(returns.columns):
        raise ValueError("trials and returns do not hold the same trial ids")
    if onc_cap < 2:
        raise ValueError("onc_cap must be at least 2, got %r" % (onc_cap,))
    flat = np.ptp(returns.to_numpy(), axis=0) == 0
    in_onc = [c for c, f in zip(returns.columns, flat) if not f]
    out = {
        "n_trials": int(len(trials)),
        "n_cleared_bar": int((trials["num_trades"] >= min_trades).sum()),
        "n_zero_variance": int(flat.sum()),
        "n_in_onc": None, "k_effective": None, "onc_cap": None, "cap_bound": None, "v": None,
        "selected_frequency": None, "reason": None, "cluster_sizes": None,
    }
    sel = returns.columns[0] if selected is None else selected
    if sel not in returns.columns:
        raise ValueError("the selected trial %r is not in the family" % (sel,))
    years = (returns.index[-1] - returns.index[0]) / pd.Timedelta(days=365.25)
    out["selected_frequency"] = float(len(returns.index) / years)
    if out["n_cleared_bar"] < 2 or len(in_onc) < 3:
        out["reason"] = REASON_TOO_FEW
        return out
    dist = onc.distance_from_returns(returns[in_onc].to_numpy())
    _order, positions, _silh = onc.cluster_distance_top(dist, max_num_clusters=min(len(in_onc) - 1, onc_cap), n_init=10, seed=seed, deadline=deadline)
    del dist
    clusters = {k: [in_onc[p] for p in members] for k, members in positions.items()}
    k = len(clusters)
    result = onc.cluster_variance(returns, clusters, sel)
    out.update({"n_in_onc": len(in_onc), "k_effective": k, "onc_cap": int(onc_cap), "cap_bound": bool(k >= onc_cap),
                "v": result["v"], "selected_frequency": result["selected_frequency"],
                "cluster_sizes": sorted(len(m) for m in clusters.values())})
    return out
