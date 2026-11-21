"""The optimal trading rule of AFML chapter 13 (lane GV2-6, plan docs/lanes/GV2-6.md).

Plain words: a strategy buys when it expects the price to come back to some level. We fit an Ornstein-Uhlenbeck
(O-U) process, a price that is pulled back to a level at a steady rate, to the strategy's own past opportunities,
simulate many paths from it, and pick the profit target and stop loss with the best Sharpe ratio on a 20 by 20
grid. The pair comes from the process, never from searching the history, so it is not a trial.

Units are log prices. An opportunity is (path, gap): path = [0, then log close minus log entry open for each
session], gap = log expected price minus log entry open (AFML page 233: P0 is fixed at 0).
Pure numpy and pandas. No vbt.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import product

import numpy as np
import pandas as pd

# AFML page 234, batch() defaults: 20 values from 0.5 to 10 for the target and for the stop, in sigma units.
MESH_MULTIPLES = np.linspace(0.5, 10, 20)


class OTRError(ValueError):
    """A bad fit or a bad input to the optimal trading rule. The message names the value."""


@dataclass(frozen=True)
class OUFit:
    phi: float          # the pull-back factor per step, inside (0, 1)
    sigma: float        # standard deviation of the step noise
    half_life: float    # steps for half the gap to close: -log(2) / log(phi), AFML page 232
    n_obs: int          # how many (X, Y) pairs the fit used


def fit_ou(opportunities) -> OUFit:
    """AFML step 1, equations 13.5 to 13.7. `opportunities` is a list of (path, gap)."""
    items = list(opportunities)
    if len(items) < 2:
        raise OTRError(f"fit_ou needs at least 2 opportunities, got {len(items)}")
    xs, ys = [], []
    for i, (path, gap) in enumerate(items):
        arr = np.asarray(path, dtype=float)
        if arr.ndim != 1 or len(arr) < 2:
            raise OTRError(f"opportunity {i}: a path needs at least 2 points in one dimension, got shape {arr.shape}")
        if not np.isfinite(arr).all() or not math.isfinite(gap):
            raise OTRError(f"opportunity {i}: the path or gap holds a NaN or infinite value (gap {gap!r})")
        xs.append(arr[:-1] - gap)
        ys.append(arr[1:] - gap)
    x, y = np.concatenate(xs), np.concatenate(ys)
    var_x = np.cov(x, x)[0, 1]
    if var_x == 0:
        raise OTRError(f"fit_ou: the variance of X is 0 over {len(x)} points, phi cannot be fitted")
    phi = float(np.cov(y, x)[0, 1] / var_x)
    if not 0 < phi < 1:
        raise OTRError(f"fit_ou: phi {phi:.6f} is not inside (0, 1), so the half-life is undefined")
    xi = y - phi * x
    sigma = float(math.sqrt(np.cov(xi, xi)[0, 1]))
    return OUFit(phi=phi, sigma=sigma, half_life=float(-math.log(2) / math.log(phi)), n_obs=int(len(x)))


def mesh(fit: OUFit, gap: float, max_steps: int, n_iter: int = 100_000, seed: int = 0) -> pd.DataFrame:
    """AFML steps 2 to 4, Snippet 13.2. One row per (target, stop) node, product order target then stop.

    p_t = (1 - phi) gap + phi p_(t-1) + sigma eps_t from p_0 = 0. A path leaves at the first step where p > target
    or p < -stop, or at step `max_steps`; its value is p then. Columns: target, stop, mean, std (ddof 0), sharpe,
    and target_sigma, stop_sigma (the same pair in sigma units).
    """
    if max_steps < 1:
        raise OTRError(f"mesh: max_steps must be at least 1, got {max_steps}")
    if n_iter < 2:
        raise OTRError(f"mesh: n_iter must be at least 2, got {n_iter}")
    if not 0 < fit.phi < 1:
        raise OTRError(f"mesh: phi {fit.phi!r} is not inside (0, 1)")
    if not (math.isfinite(fit.sigma) and fit.sigma > 0):
        raise OTRError(f"mesh: sigma must be positive and finite, got {fit.sigma!r}")
    if not math.isfinite(gap):
        raise OTRError(f"mesh: gap must be finite, got {gap!r}")

    eps = np.random.default_rng(seed).standard_normal((n_iter, max_steps))
    paths = np.empty((n_iter, max_steps))
    p = np.zeros(n_iter)
    for t in range(max_steps):
        p = (1 - fit.phi) * gap + fit.phi * p + fit.sigma * eps[:, t]
        paths[:, t] = p
    rows_index = np.arange(n_iter)
    rows = []
    for t_mult, s_mult in product(MESH_MULTIPLES, MESH_MULTIPLES):
        target, stop = float(t_mult * fit.sigma), float(s_mult * fit.sigma)
        hit = (paths > target) | (paths < -stop)
        hit[:, -1] = True  # the last step always closes the trade
        value = paths[rows_index, hit.argmax(axis=1)]
        mean, std = float(np.mean(value)), float(np.std(value))
        if std == 0:
            raise OTRError(f"mesh: std is 0 at target {target!r} stop {stop!r}, the Sharpe ratio is undefined")
        rows.append((target, stop, mean, std, mean / std, float(t_mult), float(s_mult)))
    return pd.DataFrame(rows, columns=["target", "stop", "mean", "std", "sharpe", "target_sigma", "stop_sigma"])


def optimal_rule(nodes: pd.DataFrame) -> dict:
    """AFML step 4: the node with the highest Sharpe ratio; ties go to the lower target, then the lower stop."""
    need = ["target", "stop", "sharpe", "target_sigma", "stop_sigma"]
    missing = [c for c in need if c not in nodes.columns]
    if missing:
        raise OTRError(f"optimal_rule: the mesh lacks the columns {missing}")
    if len(nodes) == 0:
        raise OTRError("optimal_rule: the mesh has 0 nodes")
    if nodes["sharpe"].isna().any():
        raise OTRError(f"optimal_rule: the mesh has {int(nodes['sharpe'].isna().sum())} NaN sharpe values")
    best = nodes.sort_values(["sharpe", "target", "stop"], ascending=[False, True, True], kind="mergesort").iloc[0]
    return {k: float(best[k]) for k in need}
