"""ONC, the optimal number of clusters, and the variance of clustered trials (lane GV2-5b).

Source: Lopez de Prado and Lewis, "Detection of False Investment Strategies Using Unsupervised Learning Methods" (2018),
section 8.1 and appendix snippets 1 and 2 for the clustering, section 8.2 and appendix A.4 (snippet 4) for the variance.
Read from the PDF pages 10 to 12 and 15 to 18, rendered.

Plain words: many of the strategies the search tried are near copies of each other, so counting them all overstates how many
independent ideas were tried. ONC groups the strategies whose returns move together and counts the groups: K. Each group is
turned into one cluster strategy (a weighted mix of its members), and the spread of the cluster strategies' Sharpe ratios gives
V, the variance the deflated Sharpe ratio needs.

Where this port differs from the snippets, on purpose, and why:
  1. Python 3: `map(...)` and `xrange` of the 2018 snippets are written as loops and `range`.
  2. KMeans gets a `random_state` (the plan: from the seed), a fresh integer for every fit drawn from one RandomState, so the
     repeated initialisations differ from each other and the whole run repeats exactly. `n_jobs` no longer exists in sklearn.
  3. The snippet's `silhouette_samples(dist, labels)` measures, with its default euclidean metric, the distance between the rows
     of `dist`: the "distance of distances" of section 8.1. Here that matrix is computed once per clustering and handed to
     silhouette_samples as a precomputed distance: the same numbers, N squared work per fit instead of N cubed.
  4. `max_num_clusters` is passed on to the re-clustering calls, as snippet 2 does (each call is capped at it and at its own N - 1).
  5. At least 3 series are needed (the silhouette needs 2 to N - 1 clusters).
  6. A `deadline` (a time.monotonic() value) stops a clustering that runs too long, with OncTimeout. The snippet has none.
"""
from __future__ import annotations

import math
import time

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import pairwise_distances_chunked, silhouette_samples


_TILDE_WORKING_MEMORY_MIB = 64  # rows of the distance of distances are built in blocks of about this many MiB (838 rows at N = 10,006)
DISTANCE_BLOCK_ROWS = 1024  # rows of the distance matrix built at once in float64 (about 134 MB at 16,428 columns); memory only, never changes a number


class OncTimeout(RuntimeError):
    """The clustering ran past its deadline."""


def _check_deadline(deadline) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise OncTimeout("the clustering passed its deadline")


def _distance_of_correlation(c: np.ndarray) -> np.ndarray:
    """sqrt((1 - c) / 2) of a float64 block, with NaN as 0, in place."""
    c[np.isnan(c)] = 0.0
    np.subtract(1.0, c, out=c)
    np.divide(c, 2.0, out=c)
    np.sqrt(c, out=c)
    return c


def distance_from_returns(values: np.ndarray) -> np.ndarray:
    """The n by n float32 (C-order) matrix sqrt((1 - c) / 2) of the columns of a T by n array, c their correlation.

    One float64 copy of the returns is centred and scaled in place (so a block of c is a plain product); each block of
    DISTANCE_BLOCK_ROWS rows is computed in float64 and rounded once into the float32 matrix. A correlation is never held in
    float32: near 1 its rounding swamps the distance. c is clipped to [-1, 1]; NaN (a NaN bar, a flat column) counts as 0.
    """
    z = np.array(values, dtype=float, order="F", copy=True)
    if z.ndim != 2:
        raise ValueError("distance_from_returns needs a T by n array, got %d dimensions" % z.ndim)
    n = z.shape[1]
    z -= z.mean(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        z /= np.sqrt(np.einsum("ij,ij->j", z, z))
    dist = np.empty((n, n), dtype=np.float32)
    for start in range(0, n, DISTANCE_BLOCK_ROWS):
        stop = min(start + DISTANCE_BLOCK_ROWS, n)
        with np.errstate(invalid="ignore"):
            block = z[:, start:stop].T @ z
        np.clip(block, -1.0, 1.0, out=block)
        dist[start:stop] = _distance_of_correlation(block)
    return dist


def _distance_from_correlation(corr0: pd.DataFrame) -> np.ndarray:
    """The float32 distance of a correlation matrix: today's formula in float64 per block of rows, rounded once."""
    n = corr0.shape[0]
    dist = np.empty((n, corr0.shape[1]), dtype=np.float32)
    for start in range(0, n, DISTANCE_BLOCK_ROWS):
        stop = min(start + DISTANCE_BLOCK_ROWS, n)
        dist[start:stop] = _distance_of_correlation(corr0.iloc[start:stop].to_numpy(dtype=float, copy=True))
    return dist


def _distance_of_distances(values: np.ndarray) -> np.ndarray:
    """The euclidean distance between the rows of `values`, filled one block of rows at a time into one N by N float32 array."""
    n = values.shape[0]
    tilde = np.empty((n, n), dtype=np.float32)
    start = 0
    for chunk in pairwise_distances_chunked(values, working_memory=_TILDE_WORKING_MEMORY_MIB):
        tilde[start:start + chunk.shape[0]] = chunk
        start += chunk.shape[0]
    np.fill_diagonal(tilde, 0.0)
    return tilde


def _tstat(silh) -> float:
    """mean / std (ddof 0, as np.std in the snippets) of a cluster's silhouette scores, in float64."""
    silh = np.asarray(silh, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        return float(np.mean(silh) / np.std(silh))


def _cluster_core(dist: np.ndarray, max_num_clusters: int, n_init: int, seed: int, deadline) -> tuple:
    """Snippet 1 on a distance matrix. Returns (order, clusters, silh): the positions that order the series by cluster,
    {cluster: [positions]}, and the silhouettes (float64, by position). KMeans fits `dist` itself and only its labels are kept."""
    n = dist.shape[0]
    if n < 3:
        raise ValueError("ONC needs at least 3 series, got %d" % n)
    top = min(int(max_num_clusters), n - 1)
    if top < 2:
        raise ValueError("max_num_clusters must allow at least 2 clusters, got %r for %d series" % (max_num_clusters, n))
    rng = np.random.RandomState(seed)
    all_labels = []
    for _init in range(n_init):
        for i in range(2, top + 1):
            _check_deadline(deadline)
            all_labels.append(KMeans(n_clusters=i, n_init=1, random_state=int(rng.randint(0, 2 ** 31 - 1))).fit(dist).labels_)
    _check_deadline(deadline)
    tilde = _distance_of_distances(dist)
    silh, labels, best = None, None, None
    for candidate in all_labels:
        _check_deadline(deadline)
        silh_ = silhouette_samples(tilde, candidate, metric="precomputed").astype(np.float64)
        t_ = _tstat(silh_)
        if silh is None or best != best or t_ > best:
            silh, labels, best = silh_, candidate, t_
    del tilde
    order = np.argsort(labels, kind="stable")
    clusters = {int(i): np.where(labels == i)[0].tolist() for i in np.unique(labels)}
    return order, clusters, silh


def _make_new_outputs(dist: np.ndarray, clusters: dict, clusters2: dict) -> tuple:
    """Snippet 2, makeNewOutputs: the kept clusters, then the re-run ones, with the silhouettes of the whole."""
    new = {}
    for i in clusters:
        new[len(new)] = list(clusters[i])
    for i in clusters2:
        new[len(new)] = list(clusters2[i])
    order = []
    for members in new.values():
        order.extend(members)
    labels = np.zeros(dist.shape[0])
    for i in new:
        labels[new[i]] = i
    tilde = _distance_of_distances(dist)
    silh_new = silhouette_samples(tilde, labels, metric="precomputed").astype(np.float64)
    del tilde
    return np.asarray(order, dtype=np.intp), new, silh_new


def cluster_distance_top(dist: np.ndarray, max_num_clusters, n_init: int = 10, seed: int = 0, deadline=None) -> tuple:
    """Snippet 2 on one float32 distance matrix, by position. Returns (order, {cluster: [positions]}, silhouettes).

    `order` puts the series in cluster order (the stable argsort of the labels, or the new clusters' positions joined). The cap
    bounds each clustering call (the top call and every re-clustering), as in the snippet. No correlation matrix is held.
    """
    n = dist.shape[0]
    cap = n - 1 if max_num_clusters is None else min(int(max_num_clusters), n - 1)
    order, clusters, silh = _cluster_core(dist, cap, n_init, seed, deadline)
    tstats = {i: _tstat(silh[clusters[i]]) for i in clusters}
    with np.errstate(invalid="ignore"):
        t_mean = float(np.mean(list(tstats.values())))
    redo = [i for i in tstats if tstats[i] < t_mean]
    if len(redo) <= 2:
        return order, clusters, silh
    pos_redo = []
    for i in redo:
        pos_redo.extend(clusters[i])
    sub = dist[np.ix_(pos_redo, pos_redo)]
    mean_redo = float(np.mean([tstats[i] for i in redo]))
    _order2, clusters2_local, _silh2 = cluster_distance_top(sub, min(cap, len(pos_redo) - 1), n_init, seed + 1, deadline)
    del sub
    clusters2 = {k: [pos_redo[p] for p in members] for k, members in clusters2_local.items()}
    order_new, clusters_new, silh_new = _make_new_outputs(dist, {i: clusters[i] for i in clusters if i not in redo}, clusters2)
    with np.errstate(invalid="ignore"):
        new_mean = float(np.mean([_tstat(silh_new[clusters_new[i]]) for i in clusters_new]))
    if new_mean <= mean_redo:
        return order, clusters, silh
    return order_new, clusters_new, silh_new


def _wrap(corr0: pd.DataFrame, order, clusters: dict, silh) -> tuple:
    named = {k: corr0.columns[members].tolist() for k, members in clusters.items()}
    return corr0.iloc[order, order], named, pd.Series(silh, index=corr0.index, dtype=float)


def cluster_k_means_base(corr0: pd.DataFrame, max_num_clusters: int = 10, n_init: int = 10, seed: int = 0, deadline=None) -> tuple:
    """Snippet 1. Returns (corr1, clusters, silh): the correlation reordered by cluster, {cluster number: [labels]}, the silhouettes."""
    order, clusters, silh = _cluster_core(_distance_from_correlation(corr0), max_num_clusters, n_init, seed, deadline)
    return _wrap(corr0, order, clusters, silh)


def cluster_k_means_top(corr0: pd.DataFrame, max_num_clusters: int | None = None, n_init: int = 10, seed: int = 0, deadline=None) -> tuple:
    """Snippet 2. Returns (corr1, clusters, silh). The cap bounds each clustering call (the top call and every re-clustering), as in the snippet."""
    order, clusters, silh = cluster_distance_top(_distance_from_correlation(corr0), max_num_clusters, n_init, seed, deadline)
    return _wrap(corr0, order, clusters, silh)


def min_variance_weights(cov, use_extended_terms: bool = True) -> np.ndarray:
    """Appendix A.4, snippet 4 (getIVP): the robust minimum variance weights of one cluster, adding to 1.

    Section 8.2 asks for w = inv(S) 1 / (1' inv(S) 1), and warns that a cluster's S may be ill-conditioned because the
    correlations inside it are high. The appendix's robust method sets the weights proportional to 1 / variance, less the
    average-correlation term of the Sherman-Morrison inverse. For two series it equals the exact weights.
    """
    cov = np.atleast_2d(np.asarray(cov, dtype=float))
    n = cov.shape[0]
    if n == 1:
        return np.array([1.0])
    diag = np.diag(cov)
    if not (diag > 0).all():
        raise ValueError("a series of a cluster has zero or negative variance; it cannot be weighted")
    ivp = 1.0 / diag
    if use_extended_terms:
        std = np.sqrt(diag)
        corr = cov / np.outer(std, std)
        rho = (corr.sum() - n) / (n ** 2 - n)
        inv_sigma = np.sqrt(ivp)
        denominator = 1.0 + (n - 1) * rho
        if denominator <= 0:
            raise ValueError("the cluster's average correlation %r makes the weights undefined" % rho)
        ivp = ivp - rho * inv_sigma * inv_sigma.sum() / denominator
    total = ivp.sum()
    if not math.isfinite(total) or total == 0:
        raise ValueError("the cluster's weights add to %r and cannot be normalised" % total)
    return ivp / total


def cluster_variance(returns: pd.DataFrame, clusters: dict, selected) -> dict:
    """Section 8.2: the variance of the clustered trials' Sharpe ratios, V.

    `returns`: bar returns, one column per trial, a DatetimeIndex. `clusters`: {cluster: [column labels]}. `selected`: the
    column of the selected strategy, whose frequency V is expressed in.
      S_k = sum of w_i r_i with minimum variance weights; SR_k = mean / std (ddof 1) of S_k;
      Years_k = (last date - first date) / 365.25 days, Frequency_k = T_k / Years_k, aSR_k = SR_k sqrt(Frequency_k);
      V = var(aSR_k, ddof 1) / Frequency of the selected strategy.
    """
    if len(clusters) < 2:
        raise ValueError("V needs at least 2 clusters, got %d" % len(clusters))
    if selected not in returns.columns:
        raise ValueError("the selected trial %r is not a column of the returns" % (selected,))
    if not isinstance(returns.index, pd.DatetimeIndex):
        raise ValueError("the returns need a DatetimeIndex")

    def frequency(index) -> float:
        years = (index[-1] - index[0]) / pd.Timedelta(days=365.25)
        if not years > 0:
            raise ValueError("the returns span %r years; a frequency needs more than 0" % years)
        return len(index) / years

    asr, sr_all, freq_all, weights = [], [], [], {}
    for k, members in clusters.items():
        sub = returns[list(members)].to_numpy(dtype=float)
        w = min_variance_weights(np.cov(sub.T, ddof=1))
        series = sub @ w
        std = series.std(ddof=1)
        if not std > 0:
            raise ValueError("cluster %r has a mixed return series with zero variance; its Sharpe ratio is undefined" % (k,))
        sr = float(series.mean() / std)
        freq = frequency(returns.index)
        sr_all.append(sr)
        freq_all.append(freq)
        asr.append(sr * math.sqrt(freq))
        weights[k] = [float(x) for x in w]
    f_selected = frequency(returns[selected].index)
    return {"v": float(np.var(asr, ddof=1)) / f_selected, "selected_frequency": f_selected, "asr": asr,
            "sr": sr_all, "frequency": freq_all, "weights": weights}
