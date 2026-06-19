"""At-commit before/after assessment.

Answers "what changed at commit C?" for very recent commits, where
change-point detection has too little post-commit data to work. The location
is known, so this is a known-location two-sample step test: local before/after
levels hugging C, with the noise scale borrowed from the surrounding history
via robust lag-1 differences. That decoupling keeps the verdict meaningful
even when only a point or two exist after C.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .adaptor import ensure_aggregated
from .models import AtConfig, CommitDelta
from .stats import apply_fdr, lag1_mad_sigma, normal_two_sided_p


def at_series(
    commit_ids: list[int],
    means: list[float],
    target_id: int,
    config: AtConfig | None = None,
    benchmark: str = "",
    metric: str = "",
    bot: str = "",
    variant: str = "",
    submetric: str = "",
    engine: str | None = None,
) -> CommitDelta | None:
    """Assess one series at a target commit. Returns None when not assessable.

    commit_ids must be sorted ascending and aligned with means. The target is
    snapped to the nearest measured commit >= target_id (the first commit that
    could contain the change). Returns None if no measured commit reaches the
    target or there is no before-history.
    """
    if config is None:
        config = AtConfig()

    ids = np.asarray(commit_ids)
    vals = np.asarray(means, dtype=float)
    if ids.size == 0:
        return None

    # Snap to the first measured commit at or after the target.
    after_mask = ids >= target_id
    if not after_mask.any():
        return None
    snap_idx = int(np.argmax(after_mask))  # first True
    snapped_id = int(ids[snap_idx])

    before_vals = vals[:snap_idx]
    after_vals = vals[snap_idx:]
    if before_vals.size == 0:
        return None

    # Sigma from pre-target history only, so the step itself never inflates it.
    hist = before_vals[-config.history :]
    sigma = lag1_mad_sigma(hist.tolist())

    # Local levels: a few commits each side, robust to single bad runs.
    before_win = before_vals[-config.pre_cap :]
    after_win = after_vals[: config.post_cap]
    before_level = float(np.median(before_win))
    after_level = float(np.median(after_win))
    n_before = int(before_win.size)
    n_after = int(after_win.size)

    step = after_level - before_level
    pct_change = step / before_level if before_level else 0.0
    snr = step / sigma if sigma > 0 else 0.0

    if sigma > 0:
        se = sigma * np.sqrt(1.0 / n_before + 1.0 / n_after)
        z = step / se if se > 0 else 0.0
        p_value = normal_two_sided_p(z)
    else:
        z = 0.0
        p_value = float("nan")

    # Confidence: sigma quality first (needs history), then post-commit support.
    if hist.size < 3 or sigma <= 0:
        confidence = "low"
    elif n_after >= 3:
        confidence = "ok"
    else:
        confidence = "tentative"

    # Newest measured commit: the step cannot be confirmed to persist yet.
    transient = snapped_id == int(ids[-1])

    spark_pre = before_vals[-config.spark_pre :]
    spark_post = after_vals[: config.spark_post]
    spark = np.concatenate([spark_pre, spark_post]).tolist()
    spark_split = int(spark_pre.size)

    return CommitDelta(
        benchmark=benchmark,
        metric=metric,
        bot=bot,
        variant=variant,
        submetric=submetric,
        engine=engine,
        snapped_commit_id=snapped_id,
        before_level=before_level,
        after_level=after_level,
        step=step,
        pct_change=pct_change,
        sigma=sigma,
        snr=snr,
        z=z,
        p_value=p_value,
        n_before=n_before,
        n_after=n_after,
        history_n=int(hist.size),
        confidence=confidence,
        transient=transient,
        spark=spark,
        spark_split=spark_split,
    )


def at_from_df(
    df: pd.DataFrame,
    target_id: int,
    config: AtConfig | None = None,
) -> list[CommitDelta]:
    """Assess each unique series in a DataFrame at the target commit.

    Groups by (bot, benchmark, test, variant, submetric[, engine]), assesses
    each, applies Benjamini-Hochberg FDR across all assessed series, and marks
    significance (FDR-significant and past the min-change / min-z thresholds).
    Sorted by |z| descending.
    """
    if config is None:
        config = AtConfig()

    df = ensure_aggregated(df)
    if df.empty:
        return []

    if "submetric" not in df.columns:
        df = df.copy()
        df["submetric"] = ""

    has_engine = "engine" in df.columns
    group_cols = ["bot", "benchmark", "test", "variant", "submetric"]
    if has_engine:
        group_cols.append("engine")

    deltas: list[CommitDelta] = []
    for group_key, group_df in df.groupby(group_cols, sort=False):
        if has_engine:
            bot, benchmark, test, variant, submetric, engine = group_key
            engine = engine or None
        else:
            bot, benchmark, test, variant, submetric = group_key
            engine = None
        group_df = group_df.sort_values("commit_id")

        delta = at_series(
            commit_ids=group_df["commit_id"].tolist(),
            means=group_df["value"].tolist(),
            target_id=target_id,
            config=config,
            benchmark=benchmark,
            metric=test,
            bot=bot,
            variant=variant,
            submetric=submetric,
            engine=engine,
        )
        if delta is not None:
            deltas.append(delta)

    if not deltas:
        return []

    fdr = apply_fdr([d.p_value for d in deltas], alpha=config.alpha)
    for delta, (p_adj, sig) in zip(deltas, fdr):
        delta.p_adj = p_adj
        delta.significant = bool(
            sig
            and abs(delta.pct_change) * 100 >= config.min_pct_change
            and abs(delta.z) >= config.min_z
        )

    deltas.sort(key=lambda d: abs(d.z), reverse=True)
    return deltas
