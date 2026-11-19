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
    assert lines[0] == "thread d8 (tid 2594876), CPU time 399 ms"
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


def test_annotate_needs_the_jitdump(server):
    # The fixture keeps the profile but not its 2.5 MB jitdump.
    with pytest.raises(ToolError, match="needs the jitdump"):
        _call(server, "samply_annotate", recording=FIXTURE, symbol="hotLoop")


def test_per_library_hotspots_preserve_function_numbers_and_canonicalize_paths(
    server, tmp_path
):
    import shutil

    other = tmp_path / "other"
    shutil.copytree(FIXTURE, other)
    _call(server, "samply_hotspots", recording=FIXTURE, n=2)
    # Parallel or follow-up by_lib=True calls must not wipe #N references,
    # nor should profiling another recording clobber FIXTURE's #N entries.
    _call(server, "samply_hotspots", recording=FIXTURE, by_lib=True)
    _call(server, "samply_hotspots", recording=str(other), n=1)
    # Resolving via profile.json.gz path hits the same canonical entry as the dir.
    text = _call(
        server,
        "samply_callers",
        recording=f"{FIXTURE}/profile.json.gz",
        symbol="#2",
        depth=1,
    )
    assert "matched: hotLoop /work/work.js:3:17" in text


def test_hotspots_sort_by_total_and_merge_tiers_on_callers_and_flamegraph(server):
    by_total = _call(server, "samply_hotspots", recording=FIXTURE, n=3, sort_by="total")
    # Entry frames have 0% self time and 100% total time, so sort_by="total"
    # surfaces them at #1 ahead of leaf hotspots.
    row1 = by_total.splitlines()[4]
    assert "0.0%" in row1 and "100.0%" in row1 and "start" in row1

    merged_callers = _call(
        server,
        "samply_callers",
        recording=FIXTURE,
        symbol="hotLoop",
        merge_tiers=True,
    )
    assert "[turbofan" not in merged_callers and "[maglev" not in merged_callers

    merged_fg = _call(
        server,
        "samply_flamegraph",
        recording=FIXTURE,
        min_pct=1,
        merge_tiers=True,
    )
    assert "[turbofan" not in merged_fg and "[maglev" not in merged_fg
