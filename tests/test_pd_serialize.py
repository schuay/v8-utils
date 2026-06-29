"""Tests for the structured (JSON) change-point serialization."""

from __future__ import annotations

from v8_utils.pd.models import ChangePoint
from v8_utils.pd.serialize import changepoint_to_dict, changepoints_to_json


def _cp(**kw) -> ChangePoint:
    base = dict(
        benchmark="jetstream3.slipstream",
        metric="Total",
        bot="mac-m3-pro-perf",
        variant="v8_default",
        submetric="",
        commit_id=200,
        prev_commit_id=197,
        direction="regression",
        cohens_d=1.2,
        pct_change=-0.05,
        p_value=0.001,
        confidence="high",
        seg_before_mean=100.0,
        seg_after_mean=95.0,
        candidates=[(198, 0.1), (200, 0.8), (201, 0.1)],
        engine="v8",
    )
    base.update(kw)
    return ChangePoint(**base)


def test_localization_confidence_is_mass_at_chosen_commit():
    # commit_id=200 carries 0.8 of the candidate mass -- that is the gate value,
    # not the 0.8 peak coincidentally also being the max here.
    d = changepoint_to_dict(_cp(), commit_store=None, default_engine=None)
    assert d["localization_confidence"] == 0.8


def test_localization_confidence_zero_when_breakpoint_not_a_candidate():
    # A breakpoint that fell below the candidate floor reads as unlocalized, so
    # the consumer's confidence gate drops it.
    cp = _cp(commit_id=205)  # 205 is not among the candidates
    d = changepoint_to_dict(cp, commit_store=None, default_engine=None)
    assert d["localization_confidence"] == 0.0


def test_shape_carries_series_and_candidates():
    d = changepoint_to_dict(_cp(), commit_store=None, default_engine=None)
    assert d["series"] == {
        "bot": "mac-m3-pro-perf",
        "benchmark": "jetstream3.slipstream",
        "metric": "Total",
        "variant": "v8_default",
        "submetric": "",
        "engine": "v8",
    }
    assert d["direction"] == "regression"
    assert d["commit_id"] == 200
    assert d["prev_commit_id"] == 197
    # Categorical series-noise tag stays distinct from localization confidence.
    assert d["confidence"] == "high"
    assert d["candidates"] == [
        {"commit_id": 198, "prob": 0.1},
        {"commit_id": 200, "prob": 0.8},
        {"commit_id": 201, "prob": 0.1},
    ]
    # No commit store -> hashes empty, but the contract keys are present.
    assert d["commit_hash"] == ""
    assert d["prev_commit_hash"] == ""


def test_engine_falls_back_to_default():
    cp = _cp(engine=None)
    d = changepoint_to_dict(cp, commit_store=None, default_engine="jsc")
    assert d["series"]["engine"] == "jsc"


def test_changepoints_to_json_maps_each():
    out = changepoints_to_json([_cp(), _cp(commit_id=201)], None, None)
    assert [c["commit_id"] for c in out] == [200, 201]
