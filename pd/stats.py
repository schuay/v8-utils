"""Statistical utilities for perf data analysis.

Works on pre-aggregated (mean, stdev, count) data using the law of
total variance for exact pooled statistics.
"""

from __future__ import annotations

import math

import pandas as pd
from scipy.stats import ttest_ind_from_stats


def combined_stats(
    means: list[float], stdevs: list[float], counts: list[int]
) -> tuple[float, float, int]:
    """Combined (mean, stdev, total_count) from per-commit aggregates.

    Uses the law of total variance:
        Var = [Σ(n_i - 1)·s_i² + Σ n_i·(m_i - M)²] / (N - 1)
    """
    N = sum(counts)
    if N == 0:
        return 0.0, 0.0, 0
    M = sum(n * m for m, n in zip(means, counts)) / N
    within = sum((n - 1) * s**2 for s, n in zip(stdevs, counts))
    between = sum(n * (m - M) ** 2 for m, n in zip(means, counts))
    var = (within + between) / max(N - 1, 1)
    return M, math.sqrt(max(var, 0.0)), N


def cohens_d(
    means_b: list[float],
    stdevs_b: list[float],
    counts_b: list[int],
    means_a: list[float],
    stdevs_a: list[float],
    counts_a: list[int],
) -> tuple[float, float]:
    """Cohen's d and Welch's t-test p-value from two segments of aggregated data."""
    m_b, s_b, n_b = combined_stats(means_b, stdevs_b, counts_b)
    m_a, s_a, n_a = combined_stats(means_a, stdevs_a, counts_a)

    denom = max(n_b + n_a - 2, 1)
    pooled = math.sqrt(((n_b - 1) * s_b**2 + (n_a - 1) * s_a**2) / denom)
    d = (m_a - m_b) / pooled if pooled > 0 else 0.0

    if n_b < 2 or n_a < 2 or (s_b == 0 and s_a == 0):
        return d, 1.0

    _, p = ttest_ind_from_stats(m_b, s_b, n_b, m_a, s_a, n_a, equal_var=False)
    return d, float(p)


def welch_p(m1: float, s1: float, n1: int, m2: float, s2: float, n2: int) -> float:
    """Welch's t-test p-value from summary statistics. Returns NaN if not computable."""
    if n1 < 2 or n2 < 2 or (s1 == 0 and s2 == 0):
        return float("nan")
    _, p = ttest_ind_from_stats(m1, s1, n1, m2, s2, n2, equal_var=False)
    return float(p)


def apply_fdr(pvalues: list[float], alpha: float = 0.05) -> list[tuple[float, bool]]:
    """Benjamini-Hochberg FDR correction. Returns list of (adjusted_p, significant)."""
    n = len(pvalues)
    if n == 0:
        return []

    indexed = [(i, p) for i, p in enumerate(pvalues)]
    valid = [(i, p) for i, p in indexed if not math.isnan(p)]
    result: list[tuple[float, bool]] = [(float("nan"), False)] * n

    if not valid:
        return result

    valid.sort(key=lambda x: x[1])
    m = len(valid)

    adj = [0.0] * m
    for rank, (orig_idx, p) in enumerate(valid):
        adj[rank] = p * m / (rank + 1)

    # Enforce monotonicity (from end)
    for j in range(m - 2, -1, -1):
        adj[j] = min(adj[j], adj[j + 1])

    for rank, (orig_idx, _) in enumerate(valid):
        adj_p = min(adj[rank], 1.0)
        result[orig_idx] = (adj_p, adj_p < alpha)

    return result
