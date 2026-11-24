"""PELT change-point detection with local robust scoring and transient cleaning.

Works on pre-aggregated (mean, stdev, count) per commit:
- Tier 2 (`detect_from_df`): drops host-wide transient commits across co-measured
  benchmark cohorts via a centered rolling-median machine index.
- Tier 1 (`detect_series`): uses PELT + local SSR refinement to propose and
  pinpoint splits, re-scores each split locally with window medians scaled by
  `lag1_mad_sigma` (`local_z`), and annotates series-wide bimodality (`flips`)
  and tail persistence (`persistent`).
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from .adaptor import ensure_aggregated, series_order
from .models import AnalysisConfig, ChangePoint
from .refine import candidate_probabilities, refine_breakpoints
from .stats import cohens_d, lag1_mad_sigma

# Tier-1 local median scoring & bimodality/persistence constants (calibrated on
# mac-mini-m6 / mac-m3 slipstream JetStream3 series).
LOCAL_WINDOW = 15
MEDIAN_SE_SCALE = 1.2533  # sqrt(pi / 2), asymptotic SE factor of sample median
FLIP_MIN_Z = 5.0
FLIP_MIN_PCT = 2.5
FLIP_SIMILARITY = 0.5
FLIP_SPAN_MULT = 3
PERSIST_FRACTION = 0.5

# Tier-2 cross-series machine-transient filter constants.
TRANSIENT_MIN_SERIES = 10
TRANSIENT_ROLLING_HALF = 7
TRANSIENT_SIGMA_MULT = 5.0
TRANSIENT_FLOOR_PCT = 1.0
_MAD_TO_SIGMA = 1.4826


def _count_sliding_flips(
    x: np.ndarray,
    pos_all: bool,
    sigma: float,
) -> int:
    """Count opposite-sign bounce pairs across sliding window medians of `x`."""
    n = len(x)
    r = LOCAL_WINDOW
    if n < 2 * r + 1 or sigma <= 0:
        return 0

    win = np.median(sliding_window_view(x, r), axis=1)
    se = sigma * MEDIAN_SE_SCALE * math.sqrt(2.0 / r)
    if se <= 0:
        return 0
    z = (win[r:] - win[: n - 2 * r + 1]) / se
    sign = np.sign(z) * (np.abs(z) >= FLIP_MIN_Z)

    steps: list[tuple[int, float]] = []
    i = 0
    while i < len(sign):
        if not sign[i]:
            i += 1
            continue
        a = i
        while i < len(sign) and sign[i] == sign[a]:
            i += 1
        pk = a + int(np.argmax(np.abs(z[a:i])))
        diff = float(win[pk + r] - win[pk])
        if pos_all:
            pct = math.expm1(diff) * 100.0
        else:
            base = float(win[pk])
            pct = (diff / base * 100.0) if base else 0.0
        if abs(pct) >= FLIP_MIN_PCT:
            steps.append((pk + r, pct))

    return sum(
        1
        for (k1, p1), (k2, p2) in itertools.pairwise(steps)
        if p1 * p2 < 0
        and abs(abs(p2) - abs(p1)) < FLIP_SIMILARITY * abs(p1)
        and k2 - k1 <= FLIP_SPAN_MULT * r
    )


def _count_segment_flips(raw_steps: list[tuple[int, float]]) -> int:
    """Fallback flip count over PELT breakpoints (handles series < 2*LOCAL_WINDOW+1)."""
    big = [(bk, pct) for bk, pct in raw_steps if abs(pct) * 100.0 >= FLIP_MIN_PCT]
    return sum(
        1
        for (k1, p1), (k2, p2) in itertools.pairwise(big)
        if p1 * p2 < 0
        and abs(abs(p2) - abs(p1)) < FLIP_SIMILARITY * abs(p1)
        and k2 - k1 <= FLIP_SPAN_MULT * LOCAL_WINDOW
    )


def _local_step_stats(
    x: np.ndarray,
    pos_all: bool,
    sigma: float,
    within_se: float,
    prev_bk: int,
    bk: int,
    next_bk: int,
    pct_change: float,
    cohens_d_val: float,
) -> tuple[float, bool, bool]:
    """Return (local_z, has_noise_scale, persistent) for a breakpoint at `bk`."""
    n_pts = len(x)
    w_bef = x[max(prev_bk, bk - LOCAL_WINDOW) : bk]
    w_aft = x[bk : min(next_bk, bk + LOCAL_WINDOW)]
    med_bef = float(np.median(w_bef))
    med_aft = float(np.median(w_aft))
    local_step = med_aft - med_bef

    inv_n = math.sqrt(1.0 / len(w_bef) + 1.0 / len(w_aft))
    if sigma > 0:
        se = sigma * MEDIAN_SE_SCALE * inv_n
        local_z = abs(local_step / se) if se > 0 else 0.0
        has_noise = True
    elif within_se > 0:
        # Constant-level synthetic segments have lag1_mad_sigma == 0; fall back
        # to the per-commit within-run standard error.
        base_lin = math.exp(med_bef) if pos_all else abs(med_bef)
        eff_sigma = (within_se / base_lin) if (pos_all and base_lin > 0) else within_se
        se = eff_sigma * MEDIAN_SE_SCALE * inv_n
        local_z = abs(local_step / se) if se > 0 else 0.0
        has_noise = True
    else:
        local_z = abs(cohens_d_val) if math.isfinite(cohens_d_val) else 0.0
        has_noise = False

    if not math.isfinite(local_z):
        local_z = 0.0

    # Clamp tail start at `bk` so steps near the right edge (< LOCAL_WINDOW
    # points from the end) never mix pre-breakpoint commits into the tail.
    tail_win = x[max(bk, n_pts - LOCAL_WINDOW) :]
    tail_step = float(np.median(tail_win)) - med_bef
    if pos_all:
        tail_pct = math.expm1(tail_step)
    else:
        tail_pct = (tail_step / med_bef) if med_bef else 0.0
    persistent = bool(
        tail_pct * pct_change > 0
        and abs(tail_pct) >= PERSIST_FRACTION * abs(pct_change)
    )
    return local_z, has_noise, persistent


def _drop_transient_commits(df: pd.DataFrame) -> pd.DataFrame:
    """Drop host-wide transient commits across co-measured benchmark cohorts.

    A thermal/load episode on a bot depresses nearly every benchmark measured
    during it, then recovers within a few commits. Centered rolling medians
    (half-width `TRANSIENT_ROLLING_HALF`) follow real persistent steps -- even
    suite-wide regressions -- with ~0 residual, while short multi-benchmark dips
    stand out in the cross-series median residual (`machine_index`). Cohorts with
    fewer than `TRANSIENT_MIN_SERIES` series are returned unchanged.
    """
    if df.empty:
        return df

    has_emb = "embedder_id" in df.columns
    cohort_cols = ["bot", "benchmark", "variant"]
    if "engine" in df.columns:
        cohort_cols.append("engine")

    bad_keys: set[tuple] = set()
    half = TRANSIENT_ROLLING_HALF
    min_support = max(3, TRANSIENT_MIN_SERIES // 2)

    for ckey, cdf in df.groupby(cohort_cols, sort=False):
        if not isinstance(ckey, tuple):
            ckey = (ckey,)
        series_groups = list(cdf.groupby(["test", "submetric"], sort=False))
        if len(series_groups) < TRANSIENT_MIN_SERIES:
            continue

        residuals: dict[tuple[int, int], list[float]] = {}
        for _, sdf in series_groups:
            sdf = sdf.sort_values(series_order(sdf))
            vals = sdf["value"].to_numpy(dtype=float)
            n = len(vals)
            if n < 3 or not (vals > 0).all():
                continue
            x = np.log(vals)
            rmed = np.empty(n)
            for i in range(n):
                rmed[i] = np.median(x[max(0, i - half) : i + half + 1])
            cids = sdf["commit_id"].tolist()
            eids = sdf["embedder_id"].tolist() if has_emb else [0] * n
            for cid, eid, res in zip(cids, eids, x - rmed):
                residuals.setdefault((int(cid), int(eid)), []).append(float(res))

        mi = {
            pt: float(np.median(rs))
            for pt, rs in residuals.items()
            if len(rs) >= min_support
        }
        if len(mi) < 3:
            continue

        mvals = np.array(list(mi.values()))
        sigma = _MAD_TO_SIGMA * float(np.median(np.abs(mvals - np.median(mvals))))
        limit = max(
            TRANSIENT_SIGMA_MULT * sigma,
            math.log1p(TRANSIENT_FLOOR_PCT / 100.0),
        )
        for (cid, eid), val in mi.items():
            if abs(val) > limit:
                bad_keys.add((*ckey, cid, eid))

    if not bad_keys:
        return df

    eids_col = df["embedder_id"].astype(int) if has_emb else [0] * len(df)
    row_keys = zip(
        *(df[c] for c in cohort_cols),
        df["commit_id"].astype(int),
        eids_col,
    )
    keep_mask = [k not in bad_keys for k in row_keys]
    return df.loc[keep_mask]


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
    engine: str | None = None,
    embedder_ids: list[int] | None = None,
) -> list[ChangePoint]:
    """Run PELT on a single aggregated time series and return change points.

    `embedder_ids`, when given, is aligned with `commit_ids` and names the
    embedder build each point ran under. Detection ignores it; each change
    point records the embedder on both sides of its breakpoint, which is what
    tells an embedder roll from engine movement (see ChangePoint).
    """
    import ruptures

    if config is None:
        config = AnalysisConfig()

    if len(commit_ids) < 4:
        return []
    if embedder_ids is None:
        embedder_ids = [0] * len(commit_ids)

    means_arr = np.array(means, dtype=float)
    stdevs_arr = np.array(stdevs, dtype=float)
    counts_arr = np.maximum(np.array(counts, dtype=float), 1.0)
    n_pts = len(commit_ids)

    # Confidence from median coefficient of variation
    valid = means_arr > 0
    if not valid.any():
        return []
    cvs = np.where(valid, stdevs_arr / means_arr, 0.0)
    median_cv = float(np.median(cvs[valid]))
    confidence = "high" if median_cv < 0.05 else "medium" if median_cv < 0.15 else "low"

    # Log-domain series and lag-1 MAD sigma for local robust scoring & flips.
    pos_all = bool(valid.all())
    x = np.log(means_arr) if pos_all else means_arr
    sigma = lag1_mad_sigma(x.tolist())
    within_se = float(np.median(stdevs_arr / np.sqrt(counts_arr)))

    # PELT on mean time series
    signal = means_arr.reshape(-1, 1)
    algo = ruptures.Pelt(model="rbf", min_size=config.min_size)
    try:
        bkps = algo.fit_predict(signal, pen=config.penalty)
    except Exception:  # noqa: BLE001
        return []

    # Refine breakpoints with local MLE
    refined_bkps, candidate_ssrs = refine_breakpoints(
        means_arr, bkps, n_pts, window=config.refine_window
    )

    # Build change points from refined breakpoints
    max_bk = n_pts - config.delay if config.delay > 0 else n_pts
    staged: list[tuple[ChangePoint, bool]] = []
    raw_seg_steps: list[tuple[int, float]] = []
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
        raw_seg_steps.append((bk, pct_change))

        # Cohen's d + p-value from aggregated stats
        d, p_value = cohens_d(
            means[prev_bk:bk],
            stdevs[prev_bk:bk],
            counts[prev_bk:bk],
            means[bk:next_bk],
            stdevs[bk:next_bk],
            counts[bk:next_bk],
        )

        local_z, has_noise, persistent = _local_step_stats(
            x,
            pos_all,
            sigma,
            within_se,
            prev_bk,
            bk,
            next_bk,
            pct_change,
            d,
        )

        if (
            abs(pct_change) * 100 < config.min_pct_change
            or abs(d) < config.min_effect_size
        ):
            prev_bk = bk
            continue

        if config.min_z > 0 and has_noise and local_z < config.min_z:
            prev_bk = bk
            continue

        if config.persistent_only and not persistent:
            prev_bk = bk
            continue

        direction = "improvement" if pct_change > 0 else "regression"
        candidates = candidate_probabilities(candidate_ssrs[i], n_pts, commit_ids)

        staged.append(
            (
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
                    engine=engine,
                    embedder_id=embedder_ids[bk],
                    prev_embedder_id=embedder_ids[bk - 1],
                    local_z=local_z,
                    persistent=persistent,
                ),
                has_noise,
            )
        )
        prev_bk = bk

    if not staged:
        return []

    series_flips = max(
        _count_sliding_flips(x, pos_all, sigma),
        _count_segment_flips(raw_seg_steps),
    )
    if config.max_flips is not None and series_flips > config.max_flips:
        return []

    results = []
    for cp, _ in staged:
        cp.flips = series_flips
        results.append(cp)
    return results


def detect_from_df(
    df: pd.DataFrame,
    config: AnalysisConfig | None = None,
) -> list[ChangePoint]:
    """Run PELT on each unique series in a DataFrame.

    Groups by (bot, benchmark, test, variant), runs detection on each group.
    The DataFrame must have value, stdev, count columns (use ensure_aggregated first).
    """
    if config is None:
        config = AnalysisConfig()

    df = ensure_aggregated(df)
    if df.empty:
        return []

    has_submetric = "submetric" in df.columns
    if not has_submetric:
        df = df.copy()
        df["submetric"] = ""

    if config.drop_transients:
        df = _drop_transient_commits(df)
        if df.empty:
            return []

    results = []
    has_engine = "engine" in df.columns
    has_embedder = "embedder_id" in df.columns
    group_cols = ["bot", "benchmark", "test", "variant", "submetric"]
    if has_engine:
        group_cols.append("engine")
    for group_key, group_df in df.groupby(group_cols, sort=False):
        if has_engine:
            bot, benchmark, test, variant, submetric, engine = group_key
            engine = engine or None
        else:
            bot, benchmark, test, variant, submetric = group_key
            engine = None
        group_df = group_df.sort_values(series_order(group_df))

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
            engine=engine,
            embedder_ids=group_df["embedder_id"].tolist() if has_embedder else None,
        )
        results.extend(cps)

    return results
