"""Tests for results table formatting and ANSI colorization."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from v8_utils import tools
from v8_utils.tools import _format_results_table, _results_header


def _row(
    name="metric1",
    base_mean=100.0,
    exp_mean=105.0,
    base_stdev=1.0,
    exp_stdev=1.0,
    p_value=0.001,
    unit="ms_smallerIsBetter",
    significant=True,
):
    return {
        "name": name,
        "base_mean": base_mean,
        "exp_mean": exp_mean,
        "base_stdev": base_stdev,
        "exp_stdev": exp_stdev,
        "base_n": 30,
        "exp_n": 30,
        "p_value": p_value,
        "significant": significant,
        "unit": unit,
    }


def _job(
    configuration="linux-perf",
    benchmark="speedometer3.crossbench",
    story="Speedometer3",
    created="2026-03-20T10:00:00",
    **kw,
):
    d = {
        "configuration": configuration,
        "benchmark": benchmark,
        "story": story,
        "created": created,
    }
    d.update(kw)
    return d


# ── Results header ────────────────────────────────────────────────────────────


class TestResultsHeader:
    @patch("v8_utils.tools.pinpoint.fetch_gerrit_subject", return_value=None)
    def test_basic(self, _mock):
        h = _results_header(_job())
        assert "bot:" in h
        assert "benchmark:" in h
        assert "date:" in h

    @patch(
        "v8_utils.tools.pinpoint.fetch_gerrit_subject",
        return_value="Fix turbofan bug",
    )
    def test_with_patch(self, _mock):
        h = _results_header(_job(experiment_patch="https://crrev.com/c/12345"))
        assert "https://crrev.com/c/12345" in h
        assert '"Fix turbofan bug"' in h

    @patch("v8_utils.tools.pinpoint.fetch_gerrit_subject", return_value=None)
    def test_with_flags(self, _mock):
        h = _results_header(
            _job(base_extra_args="--no-turbo", experiment_extra_args="--turbo")
        )
        assert "base-flags:" in h
        assert "exp-flags:" in h

    def test_empty_job(self):
        assert _results_header({}) == ""

    @patch("v8_utils.tools.pinpoint.fetch_gerrit_subject", return_value=None)
    def test_ansi_header(self, _mock):
        h = _results_header(_job(), ansi=True)
        assert "\033[1m" in h  # bold values
        assert "\033[2m" in h  # dim keys


# ── Format results table ─────────────────────────────────────────────────────


class TestFormatResultsTable:
    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_basic(self, mock_pivot):
        mock_pivot.return_value = [
            _row("parse", 100, 95, unit="ms_smallerIsBetter"),
            _row("compile", 200, 210, unit="score_biggerIsBetter"),
        ]
        t = _format_results_table("j1", show_all=True, use_cas=False, job=_job())
        assert isinstance(t, str)
        assert "parse" in t
        assert "compile" in t
        assert "chg%" in t

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_compact_omits_columns(self, mock_pivot):
        mock_pivot.return_value = [_row("m1", 100, 105)]
        t = _format_results_table(
            "j1", show_all=True, use_cas=False, compact=True, job=_job()
        )
        lines = t.splitlines()
        header = [l for l in lines if "chg%" in l][0]
        assert "sig" not in header
        assert "direction" not in header

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_show_all_false(self, mock_pivot):
        mock_pivot.return_value = [
            _row("sig_metric", significant=True),
            _row("nonsig_metric", significant=False, p_value=0.5),
        ]
        t = _format_results_table("j1", show_all=False, use_cas=False, job=_job())
        assert "sig_metric" in t
        assert "nonsig_metric" not in t
        assert "1 non-significant result omitted" in t

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_show_all_true(self, mock_pivot):
        mock_pivot.return_value = [
            _row("sig_metric", significant=True),
            _row("nonsig_metric", significant=False, p_value=0.5),
        ]
        t = _format_results_table("j1", show_all=True, use_cas=False, job=_job())
        assert "sig_metric" in t
        assert "nonsig_metric" in t
        assert "omitted" not in t

    @patch("v8_utils.tools.pinpoint.pivot_results", return_value=[])
    def test_no_results(self, _mock):
        assert _format_results_table("j1", False, False, job=_job()) is None

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_all_nonsignificant(self, mock_pivot):
        mock_pivot.return_value = [
            _row("m1", significant=False, p_value=0.5),
        ]
        t = _format_results_table("j1", show_all=False, use_cas=False, job=_job())
        assert "no statistically significant results" in t

    @patch(
        "v8_utils.tools.pinpoint.pivot_results",
        side_effect=RuntimeError("timeout"),
    )
    def test_fetch_error(self, _mock):
        t = _format_results_table("j1", False, False, job=_job())
        assert "Error: timeout" in t

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_sorted_by_pct(self, mock_pivot):
        mock_pivot.return_value = [
            _row("low", base_mean=100, exp_mean=101),  # +1%
            _row("high", base_mean=100, exp_mean=120),  # +20%
            _row("mid", base_mean=100, exp_mean=110),  # +10%
        ]
        t = _format_results_table("j1", show_all=True, use_cas=False, job=_job())
        lines = t.splitlines()
        data = [l for l in lines if any(n in l for n in ("low", "mid", "high"))]
        assert "high" in data[0]
        assert "mid" in data[1]
        assert "low" in data[2]

    @patch("v8_utils.tools.pinpoint.pivot_results_cas")
    def test_use_cas(self, mock_cas):
        mock_cas.return_value = [_row("m1")]
        with patch("v8_utils.tools.pinpoint.pivot_results") as mock_pivot:
            _format_results_table("j1", True, use_cas=True, job=_job())
        mock_cas.assert_called_once()
        mock_pivot.assert_not_called()

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_no_ansi_by_default(self, mock_pivot):
        mock_pivot.return_value = [_row("m1", 100, 95)]
        t = _format_results_table("j1", True, False, job=_job())
        assert "\033[" not in t

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_ansi_has_escape_codes(self, mock_pivot):
        """With ansi=True, output contains ANSI escape codes."""
        mock_pivot.return_value = [
            _row("m1", 100, 95, unit="ms_smallerIsBetter"),
        ]
        t = _format_results_table("j1", True, False, job=_job(), ansi=True)
        assert "\033[" in t

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_ansi_smaller_better_negative_green(self, mock_pivot):
        """Smaller-better metric with negative change gets green."""
        mock_pivot.return_value = [
            _row("m1", 100, 95, unit="ms_smallerIsBetter"),  # -5%, good
        ]
        t = _format_results_table("j1", True, False, job=_job(), ansi=True)
        # green = \033[32m
        assert "\033[32m" in t

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_ansi_smaller_better_positive_red(self, mock_pivot):
        """Smaller-better metric with positive change gets red."""
        mock_pivot.return_value = [
            _row("m1", 100, 110, unit="ms_smallerIsBetter"),  # +10%, bad
        ]
        t = _format_results_table("j1", True, False, job=_job(), ansi=True)
        # red = \033[31m
        assert "\033[31m" in t

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_ansi_bigger_better_positive_green(self, mock_pivot):
        mock_pivot.return_value = [
            _row("score", 100, 110, unit="score_biggerIsBetter"),  # +10%, good
        ]
        t = _format_results_table("j1", True, False, job=_job(), ansi=True)
        assert "\033[32m" in t

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_ansi_compact_colors(self, mock_pivot):
        """Regression test: compact mode still gets correct direction colors."""
        mock_pivot.return_value = [
            _row("parse", 100, 95, unit="ms_smallerIsBetter"),  # -5%, good
        ]
        t = _format_results_table(
            "j1", True, False, compact=True, job=_job(), ansi=True
        )
        # green for improvement
        assert "\033[32m" in t

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_ansi_header_bold(self, mock_pivot):
        mock_pivot.return_value = [_row("m1")]
        t = _format_results_table("j1", True, False, job=_job(), ansi=True)
        # bold = \033[1m (used by rich for header)
        assert "\033[1m" in t
