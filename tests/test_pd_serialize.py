"""Tests for the structured (JSON) change-point serialization."""

from __future__ import annotations

from v8_utils.pd.models import ChangePoint, CommitInfo
from v8_utils.pd.serialize import (
    changepoint_to_dict,
    changepoints_to_json,
    changepoints_to_payload,
)


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


class _FakeStore:
    """Minimal CommitStore stand-in: resolves any id to a full-length hash."""

    def __init__(self, engine: str = "v8"):
        self._engine = engine

    def get(self, engine, commit_id):
        if engine != self._engine:
            return None
        return CommitInfo(
            id=commit_id,
            hash=f"{commit_id:040x}",
            date="2026-09-01",
            timestamp=0,
            title=f"commit {commit_id}",
        )


def test_hashes_are_abbreviated():
    d = changepoint_to_dict(_cp(), _FakeStore(), None)
    assert d["commit_hash"] == f"{200:040x}"[:12]
    assert d["prev_commit_hash"] == f"{197:040x}"[:12]


def _payload(points, limit, offset=0):
    return changepoints_to_payload(points, None, None, limit, offset)


def test_page_fits_and_reports_no_truncation():
    out = _payload([_cp(), _cp(commit_id=201)], limit=10)
    assert out["total"] == 2
    assert out["returned"] == 2
    assert out["offset"] == 0
    assert out["truncated"] is False


def test_page_over_limit_is_truncated_and_reports_the_total():
    points = [_cp(commit_id=200 + i) for i in range(5)]
    out = _payload(points, limit=2)
    assert out["returned"] == 2
    assert out["total"] == 5
    assert out["truncated"] is True


def test_offset_paging_covers_every_point_exactly_once():
    # The property that makes paging safe to loop on: no point is skipped and
    # none is served twice, so a consumer draining to truncated=False sees all.
    points = [
        _cp(commit_id=200 + i, candidates=[(200 + i, 0.5 + i / 100)]) for i in range(7)
    ]
    seen, offset, pages = [], 0, 0
    while True:
        out = _payload(points, limit=3, offset=offset)
        assert out["offset"] == offset
        seen += [c["commit_id"] for c in out["changepoints"]]
        pages += 1
        if not out["truncated"]:
            break
        offset += out["returned"]
    assert pages == 3
    assert sorted(seen) == [200 + i for i in range(7)]


def test_pages_lead_with_the_best_localized_points():
    # The publish gate is localization confidence, so a short page must carry the
    # points a consumer could act on -- not whichever ones detection emitted first.
    low = _cp(commit_id=300, candidates=[(300, 0.10)])
    high = _cp(commit_id=301, candidates=[(301, 0.90)])
    mid = _cp(commit_id=302, candidates=[(302, 0.50)])
    out = _payload([low, high, mid], limit=2)
    assert [c["commit_id"] for c in out["changepoints"]] == [301, 302]


def test_order_is_total_so_equal_points_do_not_swap_between_pages():
    # Same confidence and same magnitude: without the series tiebreak these could
    # reorder between calls and one would never be returned.
    a = _cp(benchmark="aaa", commit_id=200, candidates=[(200, 0.5)])
    b = _cp(benchmark="bbb", commit_id=200, candidates=[(200, 0.5)])
    first = _payload([a, b], limit=1)["changepoints"]
    also_first = _payload([b, a], limit=1)["changepoints"]
    assert first[0]["series"]["benchmark"] == "aaa"
    assert also_first[0]["series"]["benchmark"] == "aaa"
