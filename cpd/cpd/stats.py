"""Statistical utilities for aggregated benchmark data.

All functions work on pre-aggregated (mean, stdev, count) per commit,
using the law of total variance to recover exact pooled statistics.
"""

from __future__ import annotations

import math

from scipy.stats import ttest_ind_from_stats

from .models import SeriesPoint


def combined_stats(points: list[SeriesPoint]) -> tuple[float, float, int]:
    """Combined (mean, stdev, total_count) from per-commit aggregates.

    Uses the law of total variance:
        Var = [Σ(n_i - 1)·s_i² + Σ n_i·(m_i - M)²] / (N - 1)
    This is exact — no approximation.
    """
    N = sum(p.count for p in points)
    if N == 0:
        return 0.0, 0.0, 0
    M = sum(p.count * p.mean for p in points) / N
    within = sum((p.count - 1) * p.stdev**2 for p in points)
    between = sum(p.count * (p.mean - M) ** 2 for p in points)
    var = (within + between) / max(N - 1, 1)
    return M, math.sqrt(max(var, 0.0)), N


def cohens_d(
    before: list[SeriesPoint], after: list[SeriesPoint]
) -> tuple[float, float]:
    """Cohen's d and Welch's t-test p-value from two segments of aggregated data.

    Returns (cohens_d, p_value).
    """
    m_b, s_b, n_b = combined_stats(before)
    m_a, s_a, n_a = combined_stats(after)

    denom = max(n_b + n_a - 2, 1)
    pooled = math.sqrt(((n_b - 1) * s_b**2 + (n_a - 1) * s_a**2) / denom)
    d = (m_a - m_b) / pooled if pooled > 0 else 0.0

    if n_b < 2 or n_a < 2 or s_b == 0 and s_a == 0:
        return d, 1.0

    _, p = ttest_ind_from_stats(m_b, s_b, n_b, m_a, s_a, n_a, equal_var=False)
    return d, float(p)
