"""MCP tool group selection: platform defaults and the instructions string."""

import asyncio
import importlib
import sys

import pytest

from v8_utils import perf
from v8_utils.mcp_tools import GROUPS, build_server


def _tool_names(srv) -> set[str]:
    return {t.name for t in asyncio.run(srv.list_tools())}


def _only(*groups: str) -> dict[str, bool]:
    return {n: n in groups for n in GROUPS}


@pytest.mark.parametrize(("platform", "expected"), [("linux", True), ("darwin", False)])
def test_perf_group_defaults_by_platform(monkeypatch, platform, expected):
    from v8_utils import mcp_tools

    monkeypatch.setattr(sys, "platform", platform)
    try:
        assert importlib.reload(mcp_tools).GROUPS["perf"].default is expected
    finally:
        monkeypatch.undo()
        importlib.reload(mcp_tools)


def test_perf_tools_live_in_perf_group():
    perf_tools = _tool_names(build_server(_only("perf")))
    assert perf_tools and all(n.startswith("perf_") for n in perf_tools)
    assert not any(
        n.startswith("perf_") for n in _tool_names(build_server(_only("performance")))
    )


def test_instructions_name_only_registered_groups():
    srv = build_server(_only("performance"))
    assert "run_d8" in srv.instructions
    assert "perf_*" not in srv.instructions
    assert "gerrit_*" not in srv.instructions
    assert "perf_*" in build_server(_only("perf")).instructions


@pytest.mark.parametrize(
    ("platform", "hint"), [("linux", "Install Linux perf"), ("darwin", "Linux-only")]
)
def test_missing_perf_names_the_fix(monkeypatch, platform, hint):
    monkeypatch.setattr(perf.shutil, "which", lambda _: None)
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(FileNotFoundError, match=hint):
        perf.hotspots("perf.data")
    # tma treats RuntimeError from perf as "event not recorded"; a missing
    # binary must not be mistaken for that.
    with pytest.raises(FileNotFoundError, match=hint):
        perf.tma("perf.data")
