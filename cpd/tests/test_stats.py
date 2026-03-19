"""Tests for stats.py — combined_stats and cohens_d from aggregates."""

import math

from cpd.models import SeriesPoint
from cpd.stats import cohens_d, combined_stats


def _pt(mean, stdev, count, cid=1):
    return SeriesPoint(commit_id=cid, mean=mean, stdev=stdev, count=count)


class TestCombinedStats:
    def test_single_point(self):
        m, s, n = combined_stats([_pt(10.0, 2.0, 5)])
        assert m == 10.0
        assert n == 5
        assert s == 2.0

    def test_identical_points(self):
        """Multiple commits with same mean → stdev comes from within only."""
        pts = [_pt(10.0, 1.0, 3, cid=i) for i in range(4)]
        m, s, n = combined_stats(pts)
        assert m == 10.0
        assert n == 12
        # All means equal → between-group variance is 0 → combined stdev = within stdev
        # within = 4 * (3-1) * 1.0² = 8.0
        # var = 8.0 / 11 ≈ 0.727
        assert abs(s - math.sqrt(8.0 / 11)) < 1e-10

    def test_different_means(self):
        """Two groups with different means — between-group variance contributes."""
        pts = [_pt(10.0, 0.0, 1, cid=1), _pt(20.0, 0.0, 1, cid=2)]
        m, s, n = combined_stats(pts)
        assert m == 15.0
        assert n == 2
        # within = 0, between = 1*(10-15)² + 1*(20-15)² = 50
        # var = 50 / 1 = 50
        assert abs(s - math.sqrt(50.0)) < 1e-10

    def test_matches_raw_pooling(self):
        """Verify aggregate formula matches direct computation from raw samples."""
        # Simulate: commit 1 has [10, 12, 14], commit 2 has [20, 22]
        raw = [10, 12, 14, 20, 22]
        raw_mean = sum(raw) / len(raw)
        raw_var = sum((x - raw_mean) ** 2 for x in raw) / (len(raw) - 1)

        pts = [
            _pt(mean=12.0, stdev=2.0, count=3, cid=1),
            _pt(mean=21.0, stdev=math.sqrt(2.0), count=2, cid=2),
        ]
        m, s, n = combined_stats(pts)
        assert abs(m - raw_mean) < 1e-10
        assert abs(s - math.sqrt(raw_var)) < 1e-10
        assert n == 5

    def test_empty(self):
        m, s, n = combined_stats([])
        assert (m, s, n) == (0.0, 0.0, 0)


class TestCohensD:
    def test_no_difference(self):
        before = [_pt(10.0, 1.0, 10, cid=i) for i in range(5)]
        after = [_pt(10.0, 1.0, 10, cid=i) for i in range(5, 10)]
        d, p = cohens_d(before, after)
        assert abs(d) < 1e-10
        assert p > 0.5

    def test_large_difference(self):
        before = [_pt(10.0, 0.5, 10, cid=i) for i in range(5)]
        after = [_pt(15.0, 0.5, 10, cid=i) for i in range(5, 10)]
        d, p = cohens_d(before, after)
        assert d > 5.0  # Very large effect
        assert p < 0.001

    def test_sign(self):
        before = [_pt(20.0, 1.0, 5, cid=1)]
        after = [_pt(10.0, 1.0, 5, cid=2)]
        d, _ = cohens_d(before, after)
        assert d < 0  # after < before → negative
