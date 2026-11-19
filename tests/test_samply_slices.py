"""Time slicing and iteration buckets for samply profiles and MCP tools."""

import json
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from samply_profiles import raw_profile

from v8_utils import samply
from v8_utils.mcp_tools import samply as samply_mcp
from v8_utils.render import samply as render


def _make_profile(
    tmp_path: Path,
    samples: list[tuple[list[str], int]],
    *,
    start_time_ms: float = 1000.0,
):
    raw = raw_profile(("d8", 1, [(frames, 1, cpu_us) for frames, cpu_us in samples]))
    raw["meta"]["startTime"] = start_time_ms
    # raw_profile prepends one weightless sentinel sample; place real samples at 0, 10, 20, ... ms.
    raw["threads"][0]["samples"]["time"] = [0.0] + [
        float(i * 10) for i in range(len(samples))
    ]
    return samply.Profile(raw, tmp_path / "profile.json.gz", symbolize=False)


def test_parse_ms_s_pct_and_multi_range(tmp_path):
    # 11 samples at t = 0, 10, 20, ..., 100 ms.
    samples = [
        (["JS:^early /a.js:1:1"], 1000) if i < 5 else (["JS:*late /a.js:2:1"], 1000)
        for i in range(11)
    ]
    p = _make_profile(tmp_path, samples)
    t = p.main_thread()

    assert samply.resolve_time(p, t, "10,30") == [(10.0, 30.0)]
    assert samply.resolve_time(p, t, "10ms,30ms") == [(10.0, 30.0)]
    assert samply.resolve_time(p, t, "0.01s,0.03s") == [(10.0, 30.0)]
    assert samply.resolve_time(p, t, ",20") == [(0.0, 20.0)]
    assert samply.resolve_time(p, t, "80,") == [(80.0, 100.0)]
    assert samply.resolve_time(p, t, "0%,20% 80%,100%") == [(0.0, 20.0), (80.0, 100.0)]
    # Overlapping ranges are merged and sorted.
    assert samply.resolve_time(p, t, "50,80 10,60") == [(10.0, 80.0)]

    h_early = samply.hotspots(p, time="0%,40%")
    assert [r.function.name for r in h_early.rows] == ["early /a.js:1:1"]
    assert h_early.samples == 5

    h_late = samply.hotspots(p, time="50,100")
    assert [r.function.name for r in h_late.rows] == ["late /a.js:2:1"]
    assert h_late.samples == 6

    # Same-recording diff between early and late windows.
    d = samply.diff(p, p, baseline_time="0%,40%", experiment_time="50%,100%")
    by_name = {r.function.name: (r.base_pct, r.after_pct) for r in d}
    assert by_name["early /a.js:1:1"] == (100.0, None)
    assert by_name["late /a.js:2:1"] == (None, 100.0)


def test_callers_and_flamegraph_respect_time(tmp_path):
    samples = [
        (["JS:^setup /a.js:1:1", "Builtin:Helper"], 1000),
        (["JS:^setup /a.js:1:1", "Builtin:Helper"], 1000),
        (["JS:*steady /a.js:2:1", "Builtin:Helper"], 1000),
        (["JS:*steady /a.js:2:1", "Builtin:Helper"], 1000),
    ]
    p = _make_profile(tmp_path, samples)
    c = samply.callers(p, "Helper", time="20,30")
    assert len(c.paths) == 1
    assert c.paths[0].callers[0].name == "steady /a.js:2:1"
    assert c.time == "20,30"

    fg = samply.flamegraph(p, time="0,10")
    assert all(path.frames[0].name == "setup /a.js:1:1" for path in fg.paths)
    assert fg.time == "0,10"


def test_equal_time_slices_fallback(tmp_path):
    samples = [
        (["JS:^early /a.js:1:1"], 1000) if i < 5 else (["JS:*late /a.js:2:1"], 1000)
        for i in range(10)
    ]
    p = _make_profile(tmp_path, samples)
    sl = samply.slices(p, buckets=2)
    assert sl.source == "equal-time"
    assert len(sl.slices) == 2
    assert "early" in sl.slices[0].top
    assert "late" in sl.slices[1].top
    txt = render.format_slices(p, sl)
    assert "0%..50%" in txt and "50%..100%" in txt

    with pytest.raises(ValueError, match="no iteration timings"):
        samply.hotspots(p, time="worst")


def test_sidecar_timings_tsv_with_macos_anchor(tmp_path):
    # 10 samples at t = 0, 10, ..., 90 ms; startTime = 1000.0 ms (1_000_000 us).
    # Anchor: wall_us = 1_000_000 (profile t=0) corresponds to ticks_us = 500_000.
    # Setup: 0..9ms; iter-0 (First): 10..20ms (10ms); iter-1 (Best): 30..40ms (10ms);
    # iter-2 (Worst): 50..80ms (30ms, gc!).
    samples = [
        (["JS:^setup /driver.js:1:1"], 1000),  # t=0
        (["JS:^warmup /b.js:1:1"], 1000),  # t=10 (iter 0)
        (["JS:^warmup /b.js:1:1"], 1000),  # t=20 (iter 0)
        (["JS:*fast /b.js:2:1"], 1000),  # t=30 (iter 1)
        (["JS:*fast /b.js:2:1"], 1000),  # t=40 (iter 1)
        (["Builtin:Scavenge"], 1000),  # t=50 (iter 2)
        (["Builtin:Scavenge"], 1000),  # t=60 (iter 2)
        (["Builtin:Scavenge"], 1000),  # t=70 (iter 2)
        (["Builtin:Scavenge"], 1000),  # t=80 (iter 2)
        (["JS:^teardown /driver.js:9:1"], 1000),  # t=90
    ]
    p = _make_profile(tmp_path, samples, start_time_ms=1000.0)
    (tmp_path / "timings.tsv").write_text(
        "# anchor 1000000 500000\n"
        "bench-iteration-0\t510000\t520000\n"
        "bench-iteration-1\t530000\t540000\n"
        "bench-iteration-2\t550000\t580000\n"
    )
    (tmp_path / "iterations.json").write_text(json.dumps({"worstCaseCount": 1}))

    sl = samply.slices(p)
    assert sl.source == "timings.tsv"
    by_name = {s.name: s for s in sl.slices}
    assert "warmup" in by_name["first"].top
    assert "Scavenge" in by_name["worst"].top
    assert "fast" in by_name["best"].top
    assert by_name["iters"].samples == 8  # excludes setup (t=0) and teardown (t=90)

    d = samply.diff(p, p, baseline_time="best", experiment_time="worst")
    diff_map = {r.function.name: (r.base_pct, r.after_pct) for r in d}
    assert diff_map["Scavenge"] == (None, 100.0)
    assert diff_map["fast /b.js:2:1"] == (100.0, None)


def test_native_markers_iterations(tmp_path):
    samples = [
        (["JS:^first_fn /b.js:1:1"], 1000),  # t=0
        (["JS:*fast_fn /b.js:2:1"], 1000),  # t=10
        (["Builtin:GC"], 1000),  # t=20
        (["Builtin:GC"], 1000),  # t=30
    ]
    raw = raw_profile(("d8", 1, [(frames, 1, cpu_us) for frames, cpu_us in samples]))
    t0 = raw["threads"][0]
    t0["samples"]["time"] = [0.0, 0.0, 10.0, 20.0, 30.0]
    base = len(t0["stringArray"])
    t0["stringArray"].extend(["Air-iteration-0", "Air-iteration-1", "Air-iteration-2"])
    t0["markers"] = {
        "length": 3,
        "name": [base, base + 1, base + 2],
        "startTime": [0.0, 10.0, 20.0],
        "endTime": [5.0, 15.0, 35.0],
    }
    p = samply.Profile(raw, tmp_path / "profile.json.gz", symbolize=False)
    sl = samply.slices(p)
    assert sl.source == "markers"
    h = samply.hotspots(p, time="first")
    assert [r.function.name for r in h.rows] == ["first_fn /b.js:1:1"]


def test_mcp_samply_slices_registered():
    mcp = FastMCP("test")
    samply_mcp.register(mcp)
    names = set(mcp._tool_manager._tools.keys())
    assert "samply_slices" in names
    assert (
        "time" in mcp._tool_manager._tools["samply_hotspots"].parameters["properties"]
    )
    assert (
        "baseline_time"
        in mcp._tool_manager._tools["samply_diff"].parameters["properties"]
    )
    assert (
        "time" in mcp._tool_manager._tools["samply_annotate"].parameters["properties"]
    )
