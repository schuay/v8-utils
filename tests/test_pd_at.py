"""Tests for the at-commit before/after assessment (pd at)."""

from __future__ import annotations

import pandas as pd

from v8_utils.pd.at import at_from_df, at_series
from v8_utils.pd.models import AtConfig
from v8_utils.pd.stats import lag1_mad_sigma


def _series(values, start_id=1000):
    ids = list(range(start_id, start_id + len(values)))
    return ids, list(map(float, values))


def test_lag1_mad_sigma_ignores_trend_and_single_step():
    # Pure linear trend -> noise scale ~0 (differencing removes the trend).
    trend = [float(x) for x in range(20)]
    assert lag1_mad_sigma(trend) < 1e-9

    # A single big step is one outlier difference, discarded by the MAD.
    flat_with_step = [10.0] * 10 + [20.0] * 10
    assert lag1_mad_sigma(flat_with_step) < 1e-9


def test_clear_step_is_significant():
    # Quiet baseline at 100, clean jump to 105 at the target.
    before = [100.0, 100.2, 99.8, 100.1, 99.9, 100.0, 100.1, 99.9, 100.0, 100.2]
    after = [105.0, 105.1, 104.9, 105.0]
    ids, vals = _series(before + after)
    target = ids[len(before)]  # first commit of the after-segment

    d = at_series(ids, vals, target, AtConfig())
    assert d is not None
    assert d.snapped_commit_id == target
    assert d.direction == "improvement"
    assert d.pct_change > 0.04
    assert d.n_after == 4
    assert d.confidence == "ok"
    assert not d.transient
    assert abs(d.z) > 3


def test_same_step_in_noisy_series_is_weaker():
    # Same nominal +5 step, but the baseline swings by ~5 commit-to-commit.
    before = [100, 95, 105, 96, 104, 97, 103, 98, 102, 99]
    after = [105, 100, 110, 101]
    ids, vals = _series(before + after)
    target = ids[len(before)]

    noisy = at_series(ids, vals, target, AtConfig())
    assert noisy is not None
    # High intrinsic noise -> small SNR, even though the raw step is the same.
    assert noisy.sigma > 2.0
    assert abs(noisy.snr) < 2.0


def test_newest_commit_is_transient_and_tentative():
    before = [100.0, 100.2, 99.8, 100.1, 99.9, 100.0, 100.1, 99.9, 100.0, 100.2]
    after = [105.0]  # target is the newest measured commit
    ids, vals = _series(before + after)
    target = ids[-1]

    d = at_series(ids, vals, target, AtConfig())
    assert d is not None
    assert d.n_after == 1
    assert d.transient
    assert d.confidence == "tentative"


def test_snaps_to_next_measured_commit():
    ids = [1000, 1002, 1004, 1006, 1008, 1010]
    vals = [100.0, 100.0, 100.0, 105.0, 105.0, 105.0]
    # Target 1005 is unmeasured; snap forward to 1006.
    d = at_series(ids, vals, 1005, AtConfig())
    assert d is not None
    assert d.snapped_commit_id == 1006


def test_target_after_all_data_returns_none():
    ids, vals = _series([100.0] * 5)
    assert at_series(ids, vals, 99999, AtConfig()) is None


def test_no_before_history_returns_none():
    ids, vals = _series([100.0, 101.0])
    assert at_series(ids, vals, ids[0], AtConfig()) is None


def _df(rows):
    return pd.DataFrame(
        rows,
        columns=[
            "bot",
            "benchmark",
            "test",
            "variant",
            "commit_id",
            "value",
            "stdev",
            "count",
            "commit_time",
            "git_hash",
        ],
    )


def test_at_from_df_groups_and_marks_significance():
    rows = []
    before = [100.0, 100.2, 99.8, 100.1, 99.9, 100.0, 100.1, 99.9, 100.0, 100.2]
    after = [105.0, 105.1, 104.9, 105.0]
    for cid, v in zip(range(1000, 1000 + len(before) + len(after)), before + after):
        rows.append(
            ["bot1", "bench", "Total", "default", cid, v, 0.5, 3, "2026-01-01", "h"]
        )
    deltas = at_from_df(_df(rows), 1010, AtConfig(min_pct_change=3.0))
    assert len(deltas) == 1
    assert deltas[0].significant
    assert deltas[0].metric == "Total"
