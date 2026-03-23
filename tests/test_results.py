"""Tests for results table formatting and ANSI colorization."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from v8_utils import pp
from v8_utils.tools import FormattedTable, _format_results_table, _results_header


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


# ── Format results table ─────────────────────────────────────────────────────


class TestFormatResultsTable:
    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_basic(self, mock_pivot):
        mock_pivot.return_value = [
            _row("parse", 100, 95, unit="ms_smallerIsBetter"),
            _row("compile", 200, 210, unit="score_biggerIsBetter"),
        ]
        t = _format_results_table("j1", show_all=True, use_cas=False, job=_job())
        assert isinstance(t, FormattedTable)
        assert "parse" in t.text
        assert "compile" in t.text
        assert "chg%" in t.text
        assert len(t.line_directions) == 2

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_compact_omits_columns(self, mock_pivot):
        mock_pivot.return_value = [_row("m1", 100, 105)]
        t = _format_results_table(
            "j1", show_all=True, use_cas=False, compact=True, job=_job()
        )
        lines = t.text.splitlines()
        header = [l for l in lines if "chg%" in l][0]
        assert "sig" not in header
        assert "direction" not in header
        # direction metadata still populated
        assert len(t.line_directions) == 1

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_show_all_false(self, mock_pivot):
        mock_pivot.return_value = [
            _row("sig_metric", significant=True),
            _row("nonsig_metric", significant=False, p_value=0.5),
        ]
        t = _format_results_table("j1", show_all=False, use_cas=False, job=_job())
        assert "sig_metric" in t.text
        assert "nonsig_metric" not in t.text
        assert "1 non-significant result omitted" in t.text

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_show_all_true(self, mock_pivot):
        mock_pivot.return_value = [
            _row("sig_metric", significant=True),
            _row("nonsig_metric", significant=False, p_value=0.5),
        ]
        t = _format_results_table("j1", show_all=True, use_cas=False, job=_job())
        assert "sig_metric" in t.text
        assert "nonsig_metric" in t.text
        assert "omitted" not in t.text

    @patch("v8_utils.tools.pinpoint.pivot_results", return_value=[])
    def test_no_results(self, _mock):
        assert _format_results_table("j1", False, False, job=_job()) is None

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_all_nonsignificant(self, mock_pivot):
        mock_pivot.return_value = [
            _row("m1", significant=False, p_value=0.5),
        ]
        t = _format_results_table("j1", show_all=False, use_cas=False, job=_job())
        assert "no statistically significant results" in t.text

    @patch(
        "v8_utils.tools.pinpoint.pivot_results",
        side_effect=RuntimeError("timeout"),
    )
    def test_fetch_error(self, _mock):
        t = _format_results_table("j1", False, False, job=_job())
        assert "Error: timeout" in t.text

    @patch("v8_utils.tools.pinpoint.pivot_results")
    @patch("v8_utils.tools.pinpoint.fetch_gerrit_subject", return_value="Some CL")
    def test_directions_with_multiline_header(self, _mock_gerrit, mock_pivot):
        """Header with patch line produces multiple lines; direction map
        must account for that offset."""
        mock_pivot.return_value = [
            _row("m1", 100, 95, unit="ms_smallerIsBetter"),
        ]
        job = _job(experiment_patch="https://crrev.com/c/12345")
        t = _format_results_table("j1", show_all=True, use_cas=False, job=job)
        # Find the data line
        for lineno, line in enumerate(t.text.splitlines()):
            if "m1" in line:
                assert t.line_directions.get(lineno) == "smaller-better"
                break
        else:
            pytest.fail("data line not found")

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_directions_mapped(self, mock_pivot):
        mock_pivot.return_value = [
            _row("small", unit="ms_smallerIsBetter"),
            _row("big", unit="score_biggerIsBetter"),
        ]
        t = _format_results_table("j1", show_all=True, use_cas=False, job=_job())
        dirs = set(t.line_directions.values())
        assert "smaller-better" in dirs
        assert "bigger-better" in dirs

    @patch("v8_utils.tools.pinpoint.pivot_results")
    def test_sorted_by_pct(self, mock_pivot):
        mock_pivot.return_value = [
            _row("low", base_mean=100, exp_mean=101),  # +1%
            _row("high", base_mean=100, exp_mean=120),  # +20%
            _row("mid", base_mean=100, exp_mean=110),  # +10%
        ]
        t = _format_results_table("j1", show_all=True, use_cas=False, job=_job())
        lines = t.text.splitlines()
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


# ── Colorize results ─────────────────────────────────────────────────────────


@pytest.fixture()
def _ansi(monkeypatch):
    """Replace ANSI escape codes with readable markers."""
    monkeypatch.setattr(pp, "_BOLD", "[B]")
    monkeypatch.setattr(pp, "_DIM", "[D]")
    monkeypatch.setattr(pp, "_RED", "[R]")
    monkeypatch.setattr(pp, "_GREEN", "[G]")
    monkeypatch.setattr(pp, "_CYAN", "[C]")
    monkeypatch.setattr(pp, "_RESET", "[/]")


class TestColorizeResults:
    def test_non_tty_unchanged(self, monkeypatch):
        monkeypatch.setattr(pp, "_CYAN", "")
        table = FormattedTable("some text", {})
        assert pp._colorize_results(table) == "some text"

    def test_smaller_better_negative_green(self, _ansi):
        table = FormattedTable("metric1  100  95  -5.20%  0.001", {0: "smaller-better"})
        out = pp._colorize_results(table)
        assert "[G]-5.20%[/]" in out

    def test_smaller_better_positive_red(self, _ansi):
        table = FormattedTable(
            "metric1  100  105  +3.10%  0.001", {0: "smaller-better"}
        )
        out = pp._colorize_results(table)
        assert "[R]+3.10%[/]" in out

    def test_bigger_better_positive_green(self, _ansi):
        table = FormattedTable("metric1  100  105  +3.10%  0.001", {0: "bigger-better"})
        out = pp._colorize_results(table)
        assert "[G]+3.10%[/]" in out

    def test_bigger_better_negative_red(self, _ansi):
        table = FormattedTable("metric1  100  95  -5.20%  0.001", {0: "bigger-better"})
        out = pp._colorize_results(table)
        assert "[R]-5.20%[/]" in out

    def test_compact_correct_colors(self, _ansi):
        """Regression test: compact mode has no direction column but
        line_directions is populated, so colors should still be correct."""
        text = "\n".join(
            [
                "bot: m1  benchmark: sp3",
                "",
                "metric     base±std    exp±std     chg%      p",
                "--------------------------------------------",
                "parse      100 ±1      95 ±1       -5.00%    0.0010",
            ]
        )
        table = FormattedTable(text, {4: "smaller-better"})
        out = pp._colorize_results(table)
        # -5% on a smaller-better metric = improvement = green
        assert "[G]-5.00%[/]" in out

    def test_header_bold(self, _ansi):
        table = FormattedTable("metric  base  exp  chg%  p", {})
        out = pp._colorize_results(table)
        assert "[B]" in out

    def test_separator_dim(self, _ansi):
        table = FormattedTable("----------", {})
        out = pp._colorize_results(table)
        assert "[D]----------[/]" in out

    def test_omitted_dim(self, _ansi):
        table = FormattedTable("(2 non-significant results omitted)", {})
        out = pp._colorize_results(table)
        assert "[D]" in out
