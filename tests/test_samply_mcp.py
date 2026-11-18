"""samply_* MCP tools, called through the server on the fixture recording."""

import asyncio
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from v8_utils.mcp_tools import GROUPS, build_server

FIXTURE = str(Path(__file__).parent / "fixtures" / "samply")


@pytest.fixture(scope="module")
def server():
    return build_server({n: n == "samply" for n in GROUPS})


def _call(server, name: str, **args) -> str:
    result = asyncio.run(server.call_tool(name, args))
    result = result[0] if isinstance(result, tuple) else result
    return getattr(result, "content", result)[0].text


def test_hotspots_table(server):
    text = _call(server, "samply_hotspots", recording=FIXTURE, n=2)
    lines = text.splitlines()
    assert lines[0] == "thread d8 (tid 2594876), CPU time 405 ms"
    assert "turbofan osr" in lines[4] and "/work/work.js:1:1" in lines[4]
    assert "turbofan ctx" in lines[5] and "hotLoop /work/work.js:3:17" in lines[5]
    assert "d8: /v8/out/d8 not found" in text


def test_hotspot_numbers_name_functions_in_later_calls(server):
    _call(server, "samply_hotspots", recording=FIXTURE, n=2)
    text = _call(server, "samply_callers", recording=FIXTURE, symbol="#2", depth=1)
    assert "matched: hotLoop /work/work.js:3:17" in text
    focused = _call(server, "samply_flamegraph", recording=FIXTURE, focus="#2")
    assert "focus: hotLoop /work/work.js:3:17" in focused


def test_thread_by_tid(server):
    text = _call(server, "samply_hotspots", recording=FIXTURE, thread="2594930", n=1)
    assert text.startswith("thread V8 DefaultWorke (tid 2594930)")


def test_ambiguous_thread_name_is_an_error(server):
    with pytest.raises(ToolError, match="ambiguous thread 'V8 DefaultWorke'"):
        _call(server, "samply_hotspots", recording=FIXTURE, thread="V8 DefaultWorke")


def test_threads_diff_and_flamegraph(server):
    threads = _call(server, "samply_threads", recording=FIXTURE, top=1)
    assert threads.splitlines()[2].split()[0] == "2594876"
    diff = _call(server, "samply_diff", baseline=FIXTURE, experiment=FIXTURE)
    assert all(line.startswith("   +0.0%") for line in diff.splitlines()[2:])
    flame = _call(server, "samply_flamegraph", recording=FIXTURE, min_pct=10)
    paths = flame.splitlines()[2:]
    # The native frames above the first JIT frame are left out by default.
    assert paths and all(not p.split("  ", 1)[1].startswith("start;") for p in paths)
