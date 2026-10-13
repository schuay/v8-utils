"""Embedder rolls in change-point detection.

A browser series (an engine embedded in Chrome or Safari) carries two commit
axes: the engine commit and the embedder build. Across a roll the bot measures
the last engine commit of the old build again under the new build, so that
commit appears twice. Detection is unchanged; a change point records the
embedder on both sides of its breakpoint, and that is what separates a roll
from engine movement.
"""

from __future__ import annotations

import pandas as pd

from v8_utils.pd.adaptor import ensure_aggregated, series_order
from v8_utils.pd.adaptors.skiz import _engine_from_variant
from v8_utils.pd.detect import detect_from_df
from v8_utils.pd.refine import candidate_probabilities
from v8_utils.pd.serialize import changepoint_to_dict

COLUMNS = [
    "bot",
    "benchmark",
    "test",
    "variant",
    "commit_id",
    "embedder_id",
    "value",
    "stdev",
    "count",
    "commit_time",
    "git_hash",
]


def _rows(points: list[tuple[int, int, float]]) -> pd.DataFrame:
    """One series from (commit_id, embedder_id, value) triples."""
    rows = [
        [
            "bot",
            "bench",
            "Total",
            "chrome_default",
            cid,
            eid,
            v,
            0.2,
            3,
            "2026-01-01",
            "h",
        ]
        for cid, eid, v in points
    ]
    return pd.DataFrame(rows, columns=COLUMNS)


def _bridge_series(step_at_roll: bool) -> pd.DataFrame:
    """Commits 1000..1019 under embedder 50, then 1010..1019 again under 51
    starting with the bridge row (1009, 51). The step sits either between the
    two 1009 rows (a roll) or between 1008 and 1009 (engine movement)."""
    pts = []
    for cid in range(1000, 1010):
        level = 100.0 if (step_at_roll or cid < 1009) else 105.0
        pts.append((cid, 50, level))
    pts.append((1009, 51, 105.0))
    for cid in range(1010, 1020):
        pts.append((cid, 51, 105.0))
    return _rows(pts)


def test_step_between_the_bridge_rows_is_a_roll():
    (cp,) = detect_from_df(_bridge_series(step_at_roll=True))
    assert (cp.prev_commit_id, cp.commit_id) == (1009, 1009)
    assert (cp.prev_embedder_id, cp.embedder_id) == (50, 51)
    assert cp.embedder_roll


def test_step_before_the_bridge_rows_is_engine_movement():
    (cp,) = detect_from_df(_bridge_series(step_at_roll=False))
    assert (cp.prev_commit_id, cp.commit_id) == (1008, 1009)
    assert (cp.prev_embedder_id, cp.embedder_id) == (50, 50)
    assert not cp.embedder_roll


def test_roll_point_is_localized_to_its_commit():
    # The bridge commit occupies two positions. Localization confidence is the
    # mass at the commit, summed over both, so the roll is as sharp as any
    # single-position step and clears a consumer's gate.
    (cp,) = detect_from_df(_bridge_series(step_at_roll=True))
    d = changepoint_to_dict(cp, commit_store=None, default_engine="v8")
    assert d["localization_confidence"] > 0.9
    assert d["embedder_id"] == 51
    assert d["prev_embedder_id"] == 50


def test_bridge_rows_are_ordered_old_build_first_regardless_of_input_order():
    df = _bridge_series(step_at_roll=True)
    shuffled = df.sample(frac=1, random_state=7)
    (cp,) = detect_from_df(shuffled)
    assert (cp.prev_embedder_id, cp.embedder_id) == (50, 51)


def test_series_without_an_embedder_axis_is_unchanged():
    df = _bridge_series(step_at_roll=False).drop(columns=["embedder_id"])
    df = df.drop_duplicates(["commit_id"])
    (cp,) = detect_from_df(df)
    assert (cp.prev_embedder_id, cp.embedder_id) == (0, 0)
    assert not cp.embedder_roll


def test_candidate_mass_is_summed_per_commit():
    # Positions 2 and 3 are the same commit (bridge pair); their shares merge.
    ssrs = {1: 10.0, 2: 1.0, 3: 1.0, 4: 10.0}
    commit_ids = [100, 101, 102, 102, 103]
    out = dict(
        candidate_probabilities(ssrs, n_pts=5, commit_ids=commit_ids, min_prob=0.0)
    )
    assert set(out) == {101, 102, 103}
    assert out[102] > 0.9
    assert abs(sum(out.values()) - 1.0) < 1e-9


def test_series_order_uses_the_embedder_axis_when_present():
    with_axis = pd.DataFrame(columns=["commit_id", "embedder_id"])
    without = pd.DataFrame(columns=["commit_id"])
    assert series_order(with_axis) == ["commit_id", "embedder_id"]
    assert series_order(without) == ["commit_id"]


def test_raw_samples_aggregate_per_embedder():
    # Two raw samples per (commit, embedder); the bridge commit keeps one
    # aggregate per build instead of being pooled across the roll.
    raw = pd.DataFrame(
        [
            [
                "bot",
                "bench",
                "Total",
                "chrome_default",
                9,
                50,
                100.0,
                "2026-01-01",
                "h",
            ],
            [
                "bot",
                "bench",
                "Total",
                "chrome_default",
                9,
                50,
                102.0,
                "2026-01-01",
                "h",
            ],
            [
                "bot",
                "bench",
                "Total",
                "chrome_default",
                9,
                51,
                110.0,
                "2026-01-01",
                "h",
            ],
            [
                "bot",
                "bench",
                "Total",
                "chrome_default",
                9,
                51,
                112.0,
                "2026-01-01",
                "h",
            ],
        ],
        columns=[c for c in COLUMNS if c not in ("stdev", "count")],
    )
    agg = ensure_aggregated(raw)
    assert len(agg) == 2
    assert sorted(agg["embedder_id"]) == [50, 51]
    assert sorted(agg["value"]) == [101.0, 111.0]


def test_browser_variants_map_to_the_engine_they_embed():
    assert _engine_from_variant("chrome_default") == "v8"
    assert _engine_from_variant("safari_default") == "jsc"
    assert _engine_from_variant("v8_turbolev_future") == "v8"
    assert _engine_from_variant("jsc_default") == "jsc"
    assert _engine_from_variant("default") == ""
