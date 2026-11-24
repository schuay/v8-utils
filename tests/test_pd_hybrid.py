"""Tests for Tier-1 (local_z, flips, persistent) and Tier-2 (transient cleaning)
hybrid change-point detection."""

from __future__ import annotations

import numpy as np
import pandas as pd

from v8_utils.pd import AnalysisConfig, detect_from_df, detect_series

COLUMNS = [
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
]


def _make_df(series_map: dict[str, list[tuple[int, float]]]) -> pd.DataFrame:
    rows = []
    for test, pts in series_map.items():
        for cid, val in pts:
            rows.append(
                ["bot1", "js3", test, "v8_default", cid, val, 0.5, 3, "2026-01-01", "h"]
            )
    return pd.DataFrame(rows, columns=COLUMNS)


def test_local_z_rejects_noisy_segment_mean_shift_and_keeps_sharp_step():
    rng = np.random.default_rng(42)
    # High commit-to-commit jitter (~4%), tiny within-run stdev (0.1): a mild
    # drift produces a segment split with inflated cohens_d (because within-run
    # stdev is tiny), which local_z (scaled by lag-1 MAD) rejects at min_z=8.
    n = 60
    jitter = rng.normal(0.0, 4.0, n)
    means = (100.0 + jitter + np.where(np.arange(n) >= 30, 5.5, 0.0)).tolist()
    cids = list(range(1000, 1000 + n))

    strict = AnalysisConfig(min_pct_change=3.0, min_z=8.0)
    assert detect_series(cids, means, [0.1] * n, [3] * n, config=strict) == []

    # Same 5.5% step on a low-noise (0.3%) series clears min_z=8 easily.
    quiet = (
        100.0 + rng.normal(0.0, 0.3, n) + np.where(np.arange(n) >= 30, 5.5, 0.0)
    ).tolist()
    (cp,) = detect_series(cids, quiet, [0.3] * n, [3] * n, config=strict)
    assert cp.commit_id == 1030
    assert cp.local_z >= 8.0
    assert cp.persistent is True
    assert cp.flips == 0
    assert not cp.bimodal


def test_regression_and_revert_marks_persistence_and_gates_with_persistent_only():
    rng = np.random.default_rng(7)
    n = 60
    base = 100.0 + rng.normal(0.0, 0.2, n)
    # -10% regression at index 15, reverted back to 100 at index 30.
    base[15:30] -= 10.0
    cids = list(range(2000, 2000 + n))

    cps = detect_series(cids, base.tolist(), [0.2] * n, [3] * n)
    assert [cp.commit_id for cp in cps] == [2015, 2030]
    reg, rev = cps
    # End of series is back at 100: the regression is no longer present in the
    # tail (persistent=False), while the revert back up to 100 holds (True).
    assert reg.direction == "regression" and reg.persistent is False
    assert rev.direction == "improvement" and rev.persistent is True
    assert reg.flips == 1 and not reg.bimodal

    kept = detect_series(
        cids,
        base.tolist(),
        [0.2] * n,
        [3] * n,
        config=AnalysisConfig(persistent_only=True),
    )
    assert [cp.commit_id for cp in kept] == [2030]


def test_step_near_right_edge_is_marked_persistent():
    # Step 8 commits from the end (< LOCAL_WINDOW=15): tail check must clamp at
    # the breakpoint rather than reaching into pre-step commits.
    cids = list(range(3000, 3028))
    means = [100.0] * 20 + [108.0] * 8
    (cp,) = detect_series(cids, means, [0.2] * 28, [3] * 28)
    assert cp.commit_id == 3020
    assert cp.persistent is True


def test_bimodal_series_counts_flips_and_gates_on_max_flips():
    rng = np.random.default_rng(11)
    # Four alternating 16-commit plateaus (+10% / -10% / +10% / -10%): 3 flips.
    segs = [100.0, 110.0, 100.0, 110.0, 100.0]
    vals = np.concatenate([s + rng.normal(0.0, 0.25, 16) for s in segs])
    cids = list(range(4000, 4000 + len(vals)))

    cps = detect_series(cids, vals.tolist(), [0.25] * len(vals), [3] * len(vals))
    assert len(cps) == 4
    assert all(cp.flips >= 3 and cp.bimodal for cp in cps)

    gated = detect_series(
        cids,
        vals.tolist(),
        [0.25] * len(vals),
        [3] * len(vals),
        config=AnalysisConfig(max_flips=2),
    )
    assert gated == []


def test_tier2_drops_multi_series_host_transient_but_keeps_persistent_suite_step():
    rng = np.random.default_rng(99)
    n = 100
    cids = list(range(5000, 5000 + n))

    # 12 benchmarks measured together:
    # - Commits 5020..5025 (6 commits): host thermal dip (-12% on ALL 12 items).
    # - Commit 5060 onward: real persistent +8% improvement on item_0 only.
    series_map: dict[str, list[tuple[int, float]]] = {}
    for idx in range(12):
        vals = 100.0 + rng.normal(0.0, 0.3, n)
        vals[20:26] *= 0.88  # 6-commit host-wide transient
        if idx == 0:
            vals[60:] *= 1.08  # real persistent win on item_0
        series_map[f"item_{idx}"] = list(zip(cids, vals.tolist()))

    df = _make_df(series_map)
    # Even under loose penalty=2.5, min_size=4, Tier-2 transient removal
    # suppresses the 12x2 host-dip breakpoints and reports only item_0's step.
    cps = detect_from_df(
        df, AnalysisConfig(penalty=2.5, min_size=4, min_pct_change=3.0)
    )
    assert [(cp.metric, cp.commit_id) for cp in cps] == [("item_0", 5060)]

    # With drop_transients=False under the same config, the host dip explodes
    # into false steps across every benchmark.
    raw_cps = detect_from_df(
        df,
        AnalysisConfig(
            penalty=2.5, min_size=4, min_pct_change=3.0, drop_transients=False
        ),
    )
    assert len(raw_cps) >= 20


def test_tier2_preserves_real_suite_wide_persistent_regression():
    rng = np.random.default_rng(123)
    n = 40
    cids = list(range(6000, 6000 + n))
    series_map = {}
    for idx in range(12):
        vals = 100.0 + rng.normal(0.0, 0.25, n)
        vals[20:] *= 0.92  # real -8% compiler regression across all 12 items
        series_map[f"item_{idx}"] = list(zip(cids, vals.tolist()))

    cps = detect_from_df(_make_df(series_map))
    assert len(cps) == 12
    assert all(cp.commit_id == 6020 and cp.direction == "regression" for cp in cps)
