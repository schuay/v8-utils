"""PELT change-point detection on aggregated time series.

Works on pre-aggregated (mean, stdev, count) per commit.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .adaptor import ensure_aggregated
from .models import AnalysisConfig, ChangePoint
from .refine import candidate_probabilities, refine_breakpoints
from .stats import cohens_d


def detect_series(
    commit_ids: list[int],
    means: list[float],
    stdevs: list[float],
    counts: list[int],
    config: AnalysisConfig | None = None,
    benchmark: str = "",
    metric: str = "",
    bot: str = "",
    variant: str = "",
    submetric: str = "",
) -> list[ChangePoint]:
    """Run PELT on a single aggregated time series and return change points."""
    import ruptures

    if config is None:
        config = AnalysisConfig()

    if len(commit_ids) < 4:
        return []

    means_arr = np.array(means)
    stdevs_arr = np.array(stdevs)
    n_pts = len(commit_ids)

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
        d, p_value = cohens_d(
            means[prev_bk:bk],
            stdevs[prev_bk:bk],
            counts[prev_bk:bk],
            means[bk:next_bk],
            stdevs[bk:next_bk],
            counts[bk:next_bk],
        )

        if abs(pct_change) < config.min_pct_change and abs(d) < config.min_effect_size:
            prev_bk = bk
            continue

        direction = "improvement" if pct_change > 0 else "regression"
        candidates = candidate_probabilities(candidate_ssrs[i], n_pts, commit_ids)

        results.append(
            ChangePoint(
                benchmark=benchmark,
                metric=metric,
                bot=bot,
                variant=variant,
                submetric=submetric,
                commit_id=commit_ids[bk],
                prev_commit_id=commit_ids[bk - 1],
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


def detect_from_df(
    df: pd.DataFrame,
    config: AnalysisConfig | None = None,
) -> list[ChangePoint]:
    """Run PELT on each unique series in a DataFrame.

    Groups by (bot, benchmark, test, variant), runs detection on each group.
    The DataFrame must have value, stdev, count columns (use ensure_aggregated first).
    """
    df = ensure_aggregated(df)

    has_submetric = "submetric" in df.columns
    if not has_submetric:
        df = df.copy()
        df["submetric"] = ""

    results = []
    group_cols = ["bot", "benchmark", "test", "variant", "submetric"]
    for group_key, group_df in df.groupby(group_cols, sort=False):
        bot, benchmark, test, variant, submetric = group_key
        group_df = group_df.sort_values("commit_id")

        cps = detect_series(
            commit_ids=group_df["commit_id"].tolist(),
            means=group_df["value"].tolist(),
            stdevs=group_df["stdev"].tolist(),
            counts=group_df["count"].tolist(),
            config=config,
            benchmark=benchmark,
            metric=test,
            bot=bot,
            variant=variant,
            submetric=submetric,
        )
        results.extend(cps)

    return results
