"""Tests for shared MCP tool helpers, focused on the result byte cap."""

from v8_utils.mcp_tools import _shared
from v8_utils.mcp_tools._shared import (
    _MAX_RESULT_BYTES,
    _text_result,
    _truncate_to_bytes,
)


class TestTruncateToBytes:
    def test_short_text_unchanged(self):
        assert _truncate_to_bytes("hello", 1000) == "hello"

    def test_exact_limit_unchanged(self):
        text = "a" * 100
        assert _truncate_to_bytes(text, 100) == text

    def test_over_limit_is_truncated(self):
        text = "a" * 5000
        out = _truncate_to_bytes(text, 1000)
        assert len(out.encode("utf-8")) <= 1000
        assert "[truncated:" in out
        assert out.startswith("a")

    def test_keeps_head(self):
        text = "HEAD" + "x" * 5000
        out = _truncate_to_bytes(text, 1000)
        assert out.startswith("HEAD")

    def test_multibyte_boundary_stays_valid(self):
        # Each star is 3 UTF-8 bytes; a byte-naive cut could land mid-character.
        text = "★" * 2000
        out = _truncate_to_bytes(text, 1001)
        # Must still be decodable (it already is, being a str) and within cap.
        assert len(out.encode("utf-8")) <= 1001
        assert "�" not in out  # no replacement chars from a bad cut


class TestTextResultCap:
    def test_text_result_truncates_huge_output(self):
        body = "z" * (_MAX_RESULT_BYTES + 1_000_000)
        result = _text_result(body)
        (content,) = result.content
        assert len(content.text.encode("utf-8")) <= _MAX_RESULT_BYTES + 1000
        assert "[truncated:" in content.text

    def test_text_result_passes_small_output(self):
        result = _text_result("ok")
        (content,) = result.content
        assert content.text.endswith("ok")
        assert "[truncated:" not in content.text
