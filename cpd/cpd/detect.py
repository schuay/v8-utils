"""Core PELT change-point detection on aggregated time series.

Ported from slipstream/analyzer.py, adapted to work on pre-aggregated
(mean, stdev, count) per commit instead of raw per-run scores.
"""

from __future__ import annotations

import numpy as np
import ruptures

from .models import AnalysisConfig, ChangePoint, SeriesKey, SeriesPoint
from .refine import candidate_probabilities, refine_breakpoints
from .stats import cohens_d


def detect(
    series: list[SeriesPoint],
    key: SeriesKey | None = None,
    config: AnalysisConfig | None = None,
) -> list[ChangePoint]:
    """Run PELT on an aggregated time series and return change points.

    Args:
        series: Time series of per-commit aggregated stats, ordered by commit_id.
        key: Optional series identifier attached to returned ChangePoints.
        config: Tuning parameters. Defaults to AnalysisConfig().
    """
    if config is None:
        config = AnalysisConfig()
    if key is None:
        key = SeriesKey(source="", benchmark="", metric="")

    if len(series) < 4:
        return []

    cids = [p.commit_id for p in series]
    means_arr = np.array([p.mean for p in series])
    stdevs_arr = np.array([p.stdev for p in series])
    n_pts = len(cids)

    # Confidence from median coefficient of variation
    valid = means_arr > 0
    if not valid.any():
        return []
    cvs = np.where(valid, stdevs_arr / means_arr, 0.0)
    median_cv = float(np.median(cvs[valid]))
    confidence = "high" if median_cv < 0.05 else "medium" if median_cv < 0.15 else "low"

    # PELT on mean time series
    signal = means_arr.reshape(-1, 1)
    algo = ruptures.Pelt(model="rbf", min_size=config.min_size)
    try:
        bkps = algo.fit_predict(signal, pen=config.penalty)
    except Exception:
        return []

    # Refine breakpoints with local MLE
    refined_bkps, candidate_ssrs = refine_breakpoints(
        means_arr, bkps, n_pts, window=config.refine_window
    )

    # Build change points from refined breakpoints
    max_bk = n_pts - config.delay if config.delay > 0 else n_pts
    results = []
    prev_bk = 0
    all_bkps = refined_bkps + [n_pts]

    for i, bk in enumerate(all_bkps):
        if bk >= n_pts or bk > max_bk:
            break

        next_bk = all_bkps[i + 1] if i + 1 < len(all_bkps) else n_pts
        seg_before = means_arr[prev_bk:bk]
        seg_after = means_arr[bk:next_bk]

        if len(seg_before) < 1 or len(seg_after) < 1:
            prev_bk = bk
            continue

        m_before = float(np.mean(seg_before))
        m_after = float(np.mean(seg_after))

        if m_before == 0:
            prev_bk = bk
            continue

        pct_change = (m_after - m_before) / m_before

        # Cohen's d + p-value from aggregated stats
        d, p_value = cohens_d(series[prev_bk:bk], series[bk:next_bk])

        if abs(pct_change) < config.min_pct_change and abs(d) < config.min_effect_size:
            prev_bk = bk
            continue

        direction = "improvement" if pct_change > 0 else "regression"

        candidates = candidate_probabilities(candidate_ssrs[i], n_pts, cids)

        results.append(
            ChangePoint(
                series=key,
                commit_id=cids[bk],
                prev_commit_id=cids[bk - 1],
                direction=direction,
                cohens_d=abs(d),
                pct_change=pct_change,
                p_value=p_value,
                confidence=confidence,
                seg_before_mean=m_before,
                seg_after_mean=m_after,
                candidates=candidates,
            )
        )
        prev_bk = bk

    return results
