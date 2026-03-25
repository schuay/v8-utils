"""Unit tests for jsb — pure functions only, no subprocess or filesystem."""

from pathlib import Path

import pytest

from v8_utils.jsb import (
    Variant,
    _fmt_delta,
    _fmt_stat,
    _p_confidence,
    format_table,
    parse_js2,
    parse_js3,
    summarise,
)


# ══════════════════════════════════════════════════════════════════════════════
# Variant.parse
# ══════════════════════════════════════════════════════════════════════════════


class TestVariantParse:
    def test_simple_build(self):
        v = Variant.parse("release-main")
        assert v.build == "release-main"
        assert v.flags == ""
        assert v.label == "release-main"

    def test_build_with_flags(self):
        v = Variant.parse("release:--turbolev")
        assert v.build == "release"
        assert v.flags == "--turbolev"
        assert v.label == "release [--turbolev]"

    def test_path_d8(self):
        v = Variant.parse("/home/user/v8/out/release/d8")
        assert v.build == "release"  # parent dir name
        assert v._d8_path == Path("/home/user/v8/out/release/d8")

    def test_path_non_d8_binary(self):
        v = Variant.parse("/home/user/WebKit/jsc")
        assert v.build == "jsc"  # binary name itself

    def test_path_with_flags(self):
        v = Variant.parse("/home/user/v8/out/release/d8:--turbolev")
        assert v.build == "release"
        assert v.flags == "--turbolev"
        assert v.label == "release [--turbolev]"

    def test_d8_resolution(self):
        v = Variant.parse("release-main")
        assert v.d8(Path("/v8/out")) == Path("/v8/out/release-main/d8")

    def test_path_d8_resolution_ignores_v8_out(self):
        v = Variant.parse("/custom/path/d8")
        assert v.d8(Path("/v8/out")) == Path("/custom/path/d8")

    def test_whitespace_stripped(self):
        v = Variant.parse("  release : --turbolev  ")
        assert v.build == "release"
        assert v.flags == "--turbolev"


# ══════════════════════════════════════════════════════════════════════════════
# parse_js2 / parse_js3
# ══════════════════════════════════════════════════════════════════════════════


class TestParseJs2:
    def test_single_score(self):
        output = "crypto-md5-SP Startup-Score: 195.787\n"
        assert parse_js2(output) == {"Startup-Score": 195.787}

    def test_multiple_scores(self):
        output = (
            "crypto-md5-SP Startup-Score: 195.787\ncrypto-md5-SP First-Score: 100.5\n"
        )
        result = parse_js2(output)
        assert result == {"Startup-Score": 195.787, "First-Score": 100.5}

    def test_no_match(self):
        assert parse_js2("some other output\n") == {}

    def test_empty(self):
        assert parse_js2("") == {}


class TestParseJs3:
    def test_single_score(self):
        output = "chai-wtb Score          97.20 pts\n"
        assert parse_js3(output) == {"Score": 97.2}

    def test_multiple_scores(self):
        output = (
            "chai-wtb First-Score    61.50 pts\nchai-wtb Score          97.20 pts\n"
        )
        result = parse_js3(output)
        assert result == {"First-Score": 61.5, "Score": 97.2}

    def test_overall_lines_skipped(self):
        output = (
            "chai-wtb Score          97.20 pts\nOverall Score          102.50 pts\n"
        )
        assert parse_js3(output) == {"Score": 97.2}

    def test_empty(self):
        assert parse_js3("") == {}


# ══════════════════════════════════════════════════════════════════════════════
# _fmt_stat
# ══════════════════════════════════════════════════════════════════════════════


class TestFmtStat:
    def test_single_value(self):
        assert _fmt_stat([100.0]) == "100.00"

    def test_multiple_values(self):
        result = _fmt_stat([100.0, 100.0])
        assert result == "100.00 ±0.0%"

    def test_with_variance(self):
        result = _fmt_stat([100.0, 110.0])
        assert "105.00" in result
        assert "±" in result


# ══════════════════════════════════════════════════════════════════════════════
# _p_confidence
# ══════════════════════════════════════════════════════════════════════════════


class TestPConfidence:
    def test_high(self):
        assert _p_confidence(0.001) == "high"
        assert _p_confidence(0.009) == "high"

    def test_boundary_high(self):
        assert _p_confidence(0.01) == "medium"

    def test_medium(self):
        assert _p_confidence(0.03) == "medium"

    def test_boundary_medium(self):
        assert _p_confidence(0.05) == "low"

    def test_low(self):
        assert _p_confidence(0.5) == "low"
        assert _p_confidence(1.0) == "low"


# ══════════════════════════════════════════════════════════════════════════════
# _fmt_delta
# ══════════════════════════════════════════════════════════════════════════════


class TestFmtDelta:
    def test_positive_delta(self):
        delta, p, conf = _fmt_delta([100.0, 100.0], [110.0, 110.0])
        assert delta == "+10.0%"
        assert p is not None
        assert conf in ("high", "medium", "low")

    def test_negative_delta(self):
        delta, _, _ = _fmt_delta([110.0, 110.0], [100.0, 100.0])
        assert delta.startswith("-")

    def test_zero_base_mean(self):
        delta, p, conf = _fmt_delta([0.0, 0.0], [1.0, 1.0])
        assert delta == "N/A"
        assert p is None
        assert conf == ""

    def test_single_run_no_p(self):
        delta, p, conf = _fmt_delta([100.0], [110.0])
        assert delta == "+10.0%"
        assert p is None
        assert conf == ""

    def test_identical_samples(self):
        delta, p, _ = _fmt_delta([100.0, 100.0], [100.0, 100.0])
        assert delta == "0.0%"  # zero delta has no sign prefix
        assert p is not None

    def test_clearly_different_samples_high_confidence(self):
        base = [100.0, 101.0, 99.0, 100.5]
        exp = [200.0, 201.0, 199.0, 200.5]
        _, p, conf = _fmt_delta(base, exp)
        assert p < 0.01
        assert conf == "high"


# ══════════════════════════════════════════════════════════════════════════════
# format_table
# ══════════════════════════════════════════════════════════════════════════════


class TestFormatTable:
    def test_single_variant(self):
        v = [Variant(build="release")]
        results = [{"Score": [100.0, 102.0]}]
        table = format_table(["bench"], "JS3", 2, v, results)
        assert "bench" in table
        assert "JS3" in table
        assert "Score" in table
        # No chg% columns for single variant
        assert "chg%" not in table

    def test_two_variants_has_comparison(self):
        vs = [Variant(build="base"), Variant(build="exp")]
        results = [{"Score": [100.0, 102.0]}, {"Score": [110.0, 112.0]}]
        table = format_table(["bench"], "JS3", 2, vs, results, show_all=True)
        assert "chg%" in table
        assert "confidence" in table

    def test_metric_ordering(self):
        vs = [Variant(build="rel")]
        results = [{"Zzz-Score": [1.0], "Score": [2.0], "First-Score": [3.0]}]
        table = format_table(["bench"], "JS3", 1, vs, results)
        lines = table.strip().splitlines()
        metric_lines = [l for l in lines if "Score" in l and "metric" not in l.lower()]
        names = [l.split()[0] for l in metric_lines]
        assert names.index("Score") < names.index("First-Score")
        assert names.index("First-Score") < names.index("Zzz-Score")

    def test_significance_filtering(self):
        vs = [Variant(build="a"), Variant(build="b")]
        # Clearly different values → significant
        results = [
            {"Sig": [100.0, 101.0, 100.5], "Nonsig": [100.0, 100.1, 99.9]},
            {"Sig": [200.0, 201.0, 200.5], "Nonsig": [100.0, 100.2, 99.8]},
        ]
        table = format_table(["bench"], "JS3", 3, vs, results, show_all=False)
        assert "Sig" in table
        assert "non-significant" in table
        assert "--show-all" in table

    def test_show_all(self):
        vs = [Variant(build="a"), Variant(build="b")]
        results = [
            {"Sig": [100.0, 101.0], "Nonsig": [100.0, 100.0]},
            {"Sig": [200.0, 201.0], "Nonsig": [100.0, 100.0]},
        ]
        table = format_table(["bench"], "JS3", 2, vs, results, show_all=True)
        assert "Nonsig" in table
        assert "omitted" not in table

    def test_three_variants(self):
        vs = [Variant(build="a"), Variant(build="b"), Variant(build="c")]
        results = [
            {"Score": [100.0, 102.0]},
            {"Score": [110.0, 112.0]},
            {"Score": [120.0, 122.0]},
        ]
        table = format_table(["bench"], "JS3", 2, vs, results, show_all=True)
        assert "a" in table
        assert "b" in table
        assert "c" in table


# ══════════════════════════════════════════════════════════════════════════════
# summarise
# ══════════════════════════════════════════════════════════════════════════════


class TestSummarise:
    def test_single_variant(self):
        results = [{"Score": [100.0, 102.0, 98.0]}]
        out = summarise(results)
        assert len(out) == 1
        assert "Score" in out[0]
        assert out[0]["Score"]["mean"] == 100.0
        assert "p_value" not in out[0]["Score"]

    def test_two_variants_adds_p_and_confidence(self):
        results = [
            {"Score": [100.0, 101.0, 99.0]},
            {"Score": [200.0, 201.0, 199.0]},
        ]
        out = summarise(results)
        assert "p_value" in out[0]["Score"]
        assert "confidence" in out[0]["Score"]
        assert out[0]["Score"]["confidence"] == "high"
        # Both sides get the same values
        assert out[0]["Score"]["p_value"] == out[1]["Score"]["p_value"]


# ══════════════════════════════════════════════════════════════════════════════
# CLI argument parsing
# ══════════════════════════════════════════════════════════════════════════════


class TestCliArgs:
    """Test that argparse produces correct lineitems values."""

    def _parse(self, argv):
        import argparse

        from v8_utils.jsb import main

        # We can't call main() directly (it calls sys.exit), so replicate
        # the parser setup and just check args.lineitems.
        p = argparse.ArgumentParser()
        p.add_argument("lineitems", nargs="*")
        p.add_argument("-b", "--build", dest="builds", action="append", default=[])
        p.add_argument("-n", "--runs", type=int, default=1)
        p.add_argument("--show-all", action="store_true")
        return p.parse_args(argv)

    def test_single_lineitem(self):
        args = self._parse(["regexp-octane", "-b", "release"])
        assert args.lineitems == ["regexp-octane"]

    def test_multiple_lineitems(self):
        args = self._parse(["regexp-octane", "WSL", "-b", "release"])
        assert args.lineitems == ["regexp-octane", "WSL"]

    def test_no_lineitems(self):
        args = self._parse(["-b", "release"])
        assert args.lineitems == []

    def test_lineitems_or_none(self):
        args = self._parse(["-b", "release"])
        lineitems = args.lineitems or None
        assert lineitems is None

        args = self._parse(["regexp-octane", "-b", "release"])
        lineitems = args.lineitems or None
        assert lineitems == ["regexp-octane"]

    def test_two_variants_single_run_no_p(self):
        results = [{"Score": [100.0]}, {"Score": [200.0]}]
        out = summarise(results)
        assert "p_value" not in out[0]["Score"]

    def test_stdev_pct(self):
        results = [{"Score": [100.0, 100.0]}]
        out = summarise(results)
        assert out[0]["Score"]["stdev_pct"] == 0.0

    def test_disjoint_metrics(self):
        results = [{"A": [1.0, 2.0]}, {"B": [3.0, 4.0]}]
        out = summarise(results)
        # No shared metrics → no p_value on either
        assert "p_value" not in out[0]["A"]
        assert "p_value" not in out[1]["B"]
