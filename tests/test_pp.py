"""Unit tests for pp/pinpoint logic.

Tests focus on pure functions — no network calls, no auth, no Pinpoint API.
"""

from unittest.mock import patch

import pytest

from v8_utils import changelog
from v8_utils import daemon
from v8_utils import pinpoint
from v8_utils.pinpoint import (
    _apply_significance,
    _extract_change_and_patchset,
    _extract_change_id,
    _gerrit_change_id_from_url,
    _is_cq_job,
    _job_matches_filter,
    _parse_change_patchset,
    _value_stats,
    job_id_from_url,
    user_email_variants,
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

    def test_crrev_returns_change_id(self):
        url = "https://crrev.com/c/1234567"
        assert _gerrit_change_id_from_url(url) == "1234567"


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


# ══════════════════════════════════════════════════════════════════════════════
# user_email_variants
# ══════════════════════════════════════════════════════════════════════════════


class TestUserEmailVariants:
    def test_chromium_email(self):
        result = user_email_variants("alice@chromium.org")
        assert "alice@chromium.org" in result
        assert "alice@google.com" in result

    def test_google_email(self):
        result = user_email_variants("alice@google.com")
        assert "alice@google.com" in result
        assert "alice@chromium.org" in result

    def test_no_duplicates(self):
        result = user_email_variants("alice@google.com")
        assert len(result) == len(set(result))

    def test_preserves_original_first(self):
        result = user_email_variants("alice@example.com")
        assert result[0] == "alice@example.com"


# ══════════════════════════════════════════════════════════════════════════════
# _is_cq_job
# ══════════════════════════════════════════════════════════════════════════════


class TestIsCqJob:
    def test_cq_job(self):
        job = {"arguments": {"tags": '{"origin": "CQ"}'}}
        assert _is_cq_job(job) is True

    def test_non_cq_job(self):
        job = {"arguments": {"tags": '{"origin": "user"}'}}
        assert _is_cq_job(job) is False

    def test_no_tags(self):
        job = {"arguments": {}}
        assert _is_cq_job(job) is False

    def test_empty_tags(self):
        job = {"arguments": {"tags": ""}}
        assert _is_cq_job(job) is False

    def test_malformed_json(self):
        job = {"arguments": {"tags": "not json"}}
        assert _is_cq_job(job) is False

    def test_no_arguments(self):
        job = {}
        assert _is_cq_job(job) is False


# ══════════════════════════════════════════════════════════════════════════════
# _value_stats
# ══════════════════════════════════════════════════════════════════════════════


class TestValueStats:
    def test_multiple_values(self):
        s = _value_stats([10.0, 20.0, 30.0])
        assert s["mean"] == 20.0
        assert s["n"] == 3
        assert s["stdev"] is not None

    def test_single_value(self):
        s = _value_stats([42.0])
        assert s["mean"] == 42.0
        assert s["stdev"] is None
        assert s["n"] == 1

    def test_empty(self):
        s = _value_stats([])
        assert s["mean"] is None
        assert s["stdev"] is None
        assert s["n"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# _apply_significance
# ══════════════════════════════════════════════════════════════════════════════


class TestApplySignificance:
    def _row(self, p):
        return {"p_value": p}

    def test_pinpoint_significant(self):
        rows = _apply_significance([self._row(0.005)], method="pinpoint")
        assert rows[0]["significant"] is True

    def test_pinpoint_not_significant(self):
        rows = _apply_significance([self._row(0.05)], method="pinpoint")
        assert rows[0]["significant"] is False

    def test_pinpoint_default_alpha_is_001(self):
        rows = _apply_significance([self._row(0.009)])
        assert rows[0]["significant"] is True
        rows = _apply_significance([self._row(0.011)])
        assert rows[0]["significant"] is False

    def test_pinpoint_custom_alpha(self):
        rows = _apply_significance([self._row(0.04)], method="pinpoint", alpha=0.05)
        assert rows[0]["significant"] is True

    def test_nan_p_value_pinpoint(self):
        rows = _apply_significance([self._row(float("nan"))], method="pinpoint")
        assert rows[0]["significant"] is False
        assert rows[0]["p_value"] == 1.0

    def test_fdr_method(self):
        # One clearly significant, one not
        rows = _apply_significance([self._row(0.001), self._row(0.9)], method="fdr")
        assert rows[0]["significant"] is True
        assert rows[1]["significant"] is False

    def test_fdr_nan_handling(self):
        rows = _apply_significance(
            [self._row(float("nan")), self._row(0.001)], method="fdr"
        )
        assert rows[0]["significant"] is False
        assert rows[0]["p_value"] == 1.0
        assert rows[1]["significant"] is True

    def test_empty_rows(self):
        assert _apply_significance([]) == []

    def test_all_nan_fdr(self):
        rows = _apply_significance(
            [self._row(float("nan")), self._row(float("nan"))], method="fdr"
        )
        assert all(not r["significant"] for r in rows)


# ══════════════════════════════════════════════════════════════════════════════
# changelog._format_entry
# ══════════════════════════════════════════════════════════════════════════════


class TestFormatEntry:
    def test_no_color_strips_formatting(self):
        text = changelog._format_entry("*bold* _dim_ `code`", color=False)
        assert text == "bold dim code"

    def test_color_bold(self):
        text = changelog._format_entry("*bold*", color=True)
        assert "\033[1m" in text
        assert "bold" in text

    def test_color_dim(self):
        text = changelog._format_entry("_dim_", color=True)
        assert "\033[2m" in text

    def test_color_code(self):
        text = changelog._format_entry("`code`", color=True)
        assert "\033[36m" in text

    def test_plain_text_unchanged(self):
        assert changelog._format_entry("hello", color=False) == "hello"
        assert changelog._format_entry("hello", color=True) == "hello"
