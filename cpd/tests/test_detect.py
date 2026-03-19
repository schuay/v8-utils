"""Tests for detect.py — PELT change-point detection on aggregated series."""

from cpd.detect import detect
from cpd.models import AnalysisConfig, SeriesKey, SeriesPoint


def _make_series(means: list[float], stdev: float = 0.5, count: int = 5):
    """Build a SeriesPoint list from a list of per-commit means."""
    return [
        SeriesPoint(commit_id=i, mean=m, stdev=stdev, count=count)
        for i, m in enumerate(means)
    ]


def test_no_change():
    """Flat series → no change points."""
    series = _make_series([100.0] * 20)
    key = SeriesKey(source="test", benchmark="flat", metric="Score")
    results = detect(series, key=key)
    assert results == []


def test_step_change():
    """Clear step change in the middle → at least one change point detected."""
    means = [100.0] * 15 + [120.0] * 15
    series = _make_series(means, stdev=1.0, count=10)
    key = SeriesKey(source="test", benchmark="step", metric="Score")
    results = detect(series, key=key, config=AnalysisConfig(penalty=3.0))
    assert len(results) >= 1
    # The change point should be near index 15
    cp = results[0]
    assert 12 <= cp.commit_id <= 18
    assert cp.direction == "improvement"
    assert cp.pct_change > 0.1
    assert cp.cohens_d > 1.0
    assert cp.p_value < 0.05


def test_regression():
    """Downward step → regression."""
    means = [100.0] * 15 + [80.0] * 15
    series = _make_series(means, stdev=1.0, count=10)
    key = SeriesKey(source="test", benchmark="drop", metric="Score")
    results = detect(series, key=key)
    assert len(results) >= 1
    assert results[0].direction == "regression"
    assert results[0].pct_change < -0.1


def test_too_short():
    """Series with fewer than 4 points → empty."""
    series = _make_series([100.0, 101.0, 102.0])
    assert detect(series) == []


def test_has_p_value():
    """ChangePoint includes a p_value field."""
    means = [100.0] * 15 + [130.0] * 15
    series = _make_series(means, stdev=1.0, count=10)
    results = detect(series)
    assert len(results) >= 1
    assert isinstance(results[0].p_value, float)
    assert 0.0 <= results[0].p_value <= 1.0
