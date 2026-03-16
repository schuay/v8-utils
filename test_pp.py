"""Unit tests for pp/pinpoint logic.

Tests focus on pure functions — no network calls, no auth, no Pinpoint API.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import daemon
import pinpoint
from pinpoint import (
    _extract_change_and_patchset,
    _extract_change_id,
    _gerrit_change_id_from_url,
    _job_matches_filter,
    _parse_change_patchset,
    job_id_from_url,
)


# ══════════════════════════════════════════════════════════════════════════════
# _parse_change_patchset
# ══════════════════════════════════════════════════════════════════════════════


class TestParseChangePatchset:
    def test_bare_change_id(self):
        assert _parse_change_patchset("1234567") == ("1234567", None)

    def test_change_and_patchset(self):
        assert _parse_change_patchset("1234567/3") == ("1234567", "3")

    def test_leading_slash(self):
        assert _parse_change_patchset("/1234567/2") == ("1234567", "2")

    def test_non_numeric_returns_none(self):
        assert _parse_change_patchset("c/1234567") is None

    def test_empty_returns_none(self):
        assert _parse_change_patchset("") is None

    def test_ignores_extra_segments(self):
        # only first two numeric segments matter
        result = _parse_change_patchset("1234567/3/extra")
        assert result == ("1234567", "3")


# ══════════════════════════════════════════════════════════════════════════════
# _gerrit_change_id_from_url
# ══════════════════════════════════════════════════════════════════════════════


class TestGerritChangeIdFromUrl:
    def test_canonical_url_with_patchset(self):
        url = "https://chromium-review.googlesource.com/c/v8/v8/+/1234567/3"
        assert _gerrit_change_id_from_url(url) == "v8%2Fv8~1234567"

    def test_canonical_url_without_patchset(self):
        url = "https://chromium-review.googlesource.com/c/v8/v8/+/1234567"
        assert _gerrit_change_id_from_url(url) == "v8%2Fv8~1234567"

    def test_short_url(self):
        url = "https://chromium-review.googlesource.com/1234567"
        assert _gerrit_change_id_from_url(url) == "1234567"

    def test_wrong_host_returns_none(self):
        url = "https://example.com/c/v8/v8/+/1234567"
        assert _gerrit_change_id_from_url(url) is None

    def test_crrev_returns_none(self):
        # crrev is handled by _extract_change_id, not this function
        url = "https://crrev.com/c/1234567"
        assert _gerrit_change_id_from_url(url) is None


# ══════════════════════════════════════════════════════════════════════════════
# _extract_change_id
# ══════════════════════════════════════════════════════════════════════════════


class TestExtractChangeId:
    def test_bare_number(self):
        assert _extract_change_id("1234567") == "1234567"

    def test_number_slash_patchset(self):
        assert _extract_change_id("1234567/3") == "1234567"

    def test_full_gerrit_url(self):
        assert (
            _extract_change_id(
                "https://chromium-review.googlesource.com/c/v8/v8/+/1234567/3"
            )
            == "1234567"
        )

    def test_short_gerrit_url(self):
        assert (
            _extract_change_id("https://chromium-review.googlesource.com/1234567")
            == "1234567"
        )

    def test_crrev_url(self):
        assert _extract_change_id("https://crrev.com/c/1234567/3") == "1234567"

    def test_crrev_url_no_patchset(self):
        assert _extract_change_id("https://crrev.com/c/1234567") == "1234567"

    def test_unrecognised_url_returns_none(self):
        assert _extract_change_id("https://example.com/foo") is None

    def test_whitespace_stripped(self):
        assert _extract_change_id("  1234567  ") == "1234567"


# ══════════════════════════════════════════════════════════════════════════════
# _extract_change_and_patchset
# ══════════════════════════════════════════════════════════════════════════════


class TestExtractChangeAndPatchset:
    def test_bare_number(self):
        assert _extract_change_and_patchset("1234567") == ("1234567", None)

    def test_number_slash_patchset(self):
        assert _extract_change_and_patchset("1234567/3") == ("1234567", "3")

    def test_full_gerrit_url_with_patchset(self):
        url = "https://chromium-review.googlesource.com/c/v8/v8/+/1234567/3"
        assert _extract_change_and_patchset(url) == ("1234567", "3")

    def test_full_gerrit_url_no_patchset(self):
        url = "https://chromium-review.googlesource.com/c/v8/v8/+/1234567"
        assert _extract_change_and_patchset(url) == ("1234567", None)

    def test_crrev_url_with_patchset(self):
        assert _extract_change_and_patchset("https://crrev.com/c/1234567/3") == (
            "1234567",
            "3",
        )

    def test_crrev_url_no_patchset(self):
        assert _extract_change_and_patchset("https://crrev.com/c/1234567") == (
            "1234567",
            None,
        )

    def test_unrecognised_url(self):
        assert _extract_change_and_patchset("https://example.com/foo") is None


# ══════════════════════════════════════════════════════════════════════════════
# job_id_from_url
# ══════════════════════════════════════════════════════════════════════════════


class TestJobIdFromUrl:
    def test_full_pinpoint_url(self):
        url = "https://pinpoint-dot-chromeperf.appspot.com/job/abc123def"
        assert job_id_from_url(url) == "abc123def"

    def test_bare_id_passthrough(self):
        assert job_id_from_url("abc123") == "abc123"

    def test_url_without_job_segment(self):
        # no /job/ in path — returns input unchanged
        url = "https://example.com/other/abc123"
        assert job_id_from_url(url) == url


# ══════════════════════════════════════════════════════════════════════════════
# _job_matches_filter
# ══════════════════════════════════════════════════════════════════════════════


def _make_job(
    status="Completed",
    benchmark="speedometer3",
    configuration="linux-r350-perf",
    comparison_mode="try",
    experiment_patch=None,
):
    return {
        "status": status,
        "configuration": configuration,
        "comparison_mode": comparison_mode,
        "arguments": {
            "benchmark": benchmark,
            "experiment_patch": experiment_patch,
        },
    }


class TestJobMatchesFilter:
    def test_no_equals_always_matches(self):
        assert _job_matches_filter(_make_job(), "Completed") is True

    def test_status_match(self):
        assert _job_matches_filter(_make_job(status="Completed"), "status=Completed")

    def test_status_substring(self):
        assert _job_matches_filter(_make_job(status="Completed"), "status=omplete")

    def test_status_no_match(self):
        assert not _job_matches_filter(_make_job(status="Failed"), "status=Completed")

    def test_benchmark_match(self):
        assert _job_matches_filter(
            _make_job(benchmark="speedometer3"), "benchmark=speedometer3"
        )

    def test_configuration_match(self):
        assert _job_matches_filter(
            _make_job(configuration="linux-r350-perf"), "configuration=linux"
        )

    def test_comparison_mode_match(self):
        assert _job_matches_filter(
            _make_job(comparison_mode="try"), "comparison_mode=try"
        )

    def test_patch_bare_id_matches_full_url(self):
        job = _make_job(
            experiment_patch="https://chromium-review.googlesource.com/c/v8/v8/+/1234567/3"
        )
        assert _job_matches_filter(job, "patch=1234567")

    def test_patch_full_url_matches_bare_id(self):
        job = _make_job(
            experiment_patch="https://chromium-review.googlesource.com/1234567"
        )
        url_filter = "patch=https://chromium-review.googlesource.com/c/v8/v8/+/1234567"
        assert _job_matches_filter(job, url_filter)

    def test_patch_crrev_matches_gerrit_url(self):
        job = _make_job(
            experiment_patch="https://chromium-review.googlesource.com/c/v8/v8/+/1234567"
        )
        assert _job_matches_filter(job, "patch=https://crrev.com/c/1234567")

    def test_patch_wrong_id_no_match(self):
        job = _make_job(
            experiment_patch="https://chromium-review.googlesource.com/c/v8/v8/+/1234567"
        )
        assert not _job_matches_filter(job, "patch=9999999")

    def test_unknown_key_no_match(self):
        assert not _job_matches_filter(_make_job(), "unknownkey=value")

    def test_benchmark_alias(self):
        job = _make_job(benchmark="jetstream-main.crossbench")
        assert _job_matches_filter(job, "benchmark=js3")

    def test_benchmark_alias_no_match(self):
        job = _make_job(benchmark="speedometer3.crossbench")
        assert not _job_matches_filter(job, "benchmark=js3")

    def test_bot_alias(self):
        job = _make_job(configuration="mac-m1_mini_2020-perf")
        assert _job_matches_filter(job, "bot=m1")

    def test_bot_alias_no_match(self):
        job = _make_job(configuration="linux-r350-perf")
        assert not _job_matches_filter(job, "bot=m1")

    def test_patch_with_patchset_matches_same_patchset(self):
        job = _make_job(
            experiment_patch="https://chromium-review.googlesource.com/c/v8/v8/+/1234567/3"
        )
        assert _job_matches_filter(job, "patch=1234567/3")

    def test_patch_with_patchset_no_match_different_patchset(self):
        job = _make_job(
            experiment_patch="https://chromium-review.googlesource.com/c/v8/v8/+/1234567/3"
        )
        assert not _job_matches_filter(job, "patch=1234567/1")

    def test_patch_without_patchset_matches_any_patchset(self):
        job = _make_job(
            experiment_patch="https://chromium-review.googlesource.com/c/v8/v8/+/1234567/3"
        )
        assert _job_matches_filter(job, "patch=1234567")

    def test_patch_patchset_url_form(self):
        job = _make_job(
            experiment_patch="https://chromium-review.googlesource.com/c/v8/v8/+/1234567/3"
        )
        assert _job_matches_filter(job, "patch=https://crrev.com/c/1234567/3")
        assert not _job_matches_filter(job, "patch=https://crrev.com/c/1234567/1")


# ══════════════════════════════════════════════════════════════════════════════
# daemon._format_results_for_chat
# ══════════════════════════════════════════════════════════════════════════════


class TestFormatResultsForChat:
    def _row(self, name, base, exp, unit="ms_smallerIsBetter", significant=True):
        return {
            "name": name,
            "base_mean": base,
            "exp_mean": exp,
            "unit": unit,
            "significant": significant,
        }

    def test_no_significant_results(self):
        text = daemon._format_results_for_chat(
            [self._row("m", 1, 2, significant=False)]
        )
        assert "No statistically significant" in text

    def test_improvement_gets_green(self):
        # smaller is better, exp < base → improvement
        row = self._row("Score", 100, 80, unit="ms_smallerIsBetter")
        text = daemon._format_results_for_chat([row])
        assert "🟢" in text

    def test_regression_gets_red(self):
        # smaller is better, exp > base → regression
        row = self._row("Score", 80, 100, unit="ms_smallerIsBetter")
        text = daemon._format_results_for_chat([row])
        assert "🔴" in text

    def test_bigger_is_better_improvement(self):
        row = self._row("Score", 100, 120, unit="unitless_biggerIsBetter")
        text = daemon._format_results_for_chat([row])
        assert "🟢" in text

    def test_pct_shown(self):
        row = self._row("Score", 100, 110, unit="ms_smallerIsBetter")
        text = daemon._format_results_for_chat([row])
        assert "+10.0%" in text

    def test_only_significant_shown(self):
        rows = [
            self._row("sig", 100, 80, significant=True),
            self._row("insig", 100, 200, significant=False),
        ]
        text = daemon._format_results_for_chat(rows)
        assert "sig" in text
        assert "insig" not in text


# ══════════════════════════════════════════════════════════════════════════════
# daemon._message_text
# ══════════════════════════════════════════════════════════════════════════════


class TestMessageText:
    def _job(self, status="Completed", job_id="abc123", name="My Job"):
        return {"status": status, "job_id": job_id, "name": name}

    def test_contains_status(self):
        text = daemon._message_text(self._job(status="Failed"))
        assert "Failed" in text

    def test_contains_url(self):
        text = daemon._message_text(self._job(job_id="abc123"))
        assert "abc123" in text

    def test_contains_show_cmd(self):
        text = daemon._message_text(self._job(job_id="abc123"))
        assert "pp show-results abc123" in text

    def test_completed_icon(self):
        assert "✅" in daemon._message_text(self._job(status="Completed"))

    def test_failed_icon(self):
        assert "❌" in daemon._message_text(self._job(status="Failed"))

    def test_exception_shown(self):
        job = {**self._job(status="Failed"), "exception": "Build timeout"}
        text = daemon._message_text(job)
        assert "Build timeout" in text

    def test_results_appended(self):
        row = {
            "name": "Score",
            "base_mean": 100,
            "exp_mean": 80,
            "unit": "ms_smallerIsBetter",
            "significant": True,
        }
        text = daemon._message_text(self._job(), results=[row])
        assert "Results" in text
        assert "Score" in text
