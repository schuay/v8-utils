"""Tests for lazy dimension-value validation (bad bot/benchmark/variant names)."""

from __future__ import annotations

import pytest

from v8_utils.pd.adaptor import check_dimension_values


class _FakeAdaptor:
    """Adaptor exposing a fixed vocabulary per dimension column."""

    def __init__(self, values: dict[str, list[str]]):
        self._values = values

    def distinct_values(self, column: str) -> list[str]:
        return list(self._values.get(column, []))


_ADAPTOR = _FakeAdaptor(
    {
        "bot": ["mac-m3-jgruber", "mac-m1-jgruber"],
        "benchmark": ["jetstream3.slipstream", "speedometer3"],
        "variant": ["v8_default", "v8_turbolev"],
        "test": ["Total", "Average"],
    }
)


def test_bad_benchmark_raises_with_available_list():
    with pytest.raises(ValueError) as e:
        check_dimension_values(_ADAPTOR, {"benchmark": "jetstream3.slipstrem"})
    msg = str(e.value)
    assert "Unknown benchmark 'jetstream3.slipstrem'" in msg
    # Full list, sorted.
    assert "jetstream3.slipstream, speedometer3" in msg


def test_valid_names_do_not_raise():
    check_dimension_values(
        _ADAPTOR,
        {"benchmark": "jetstream3.slipstream", "bot": "mac-m3-jgruber"},
    )


def test_each_bad_dimension_reported_independently():
    with pytest.raises(ValueError) as e:
        check_dimension_values(_ADAPTOR, {"bot": "nope", "benchmark": "also-nope"})
    msg = str(e.value)
    assert "Unknown bot 'nope'" in msg
    assert "Unknown benchmark 'also-nope'" in msg


def test_test_column_is_not_validated():
    # `test` is a glob dimension; an exact value that isn't a known test must
    # not be flagged even though the adaptor could enumerate it.
    check_dimension_values(_ADAPTOR, {"test": "Tot*"})


def test_variant_validated():
    with pytest.raises(ValueError) as e:
        check_dimension_values(_ADAPTOR, {"variant": "v8_turbofan"})
    assert "Unknown variant 'v8_turbofan'" in str(e.value)


def test_adaptor_without_distinct_values_skips():
    class _Bare:
        pass

    # No distinct_values -> no validation, no crash.
    check_dimension_values(_Bare(), {"benchmark": "whatever"})


def test_empty_vocabulary_skips():
    # A column the source has no values for cannot distinguish typo from empty.
    adaptor = _FakeAdaptor({"benchmark": []})
    check_dimension_values(adaptor, {"benchmark": "anything"})


def test_lookup_error_skips_that_column():
    class _Boom:
        def distinct_values(self, column: str) -> list[str]:
            raise RuntimeError("spanner down")

    # A failing lookup must not convert a soft empty result into a hard error.
    check_dimension_values(_Boom(), {"benchmark": "anything"})


def test_unfiltered_dimension_not_checked():
    # Only columns present in the filter dict are validated.
    check_dimension_values(_ADAPTOR, {})
