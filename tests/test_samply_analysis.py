"""samply analyses on synthetic profiles with known stacks and weights."""

from pathlib import Path

import pytest
from samply_profiles import JIT, raw_profile

from v8_utils import samply
from v8_utils.samply import Function, Profile


def _profile(*threads) -> Profile:
    return Profile(raw_profile(*threads), Path("/rec/profile.json.gz"), symbolize=False)


MAIN = "JS:~main a.js:1:1"
F_OPT = "JS:*f a.js:2:1"
F_MAG = "JS:+f a.js:2:1"
G = "JS:*g a.js:3:1"
BUILTIN = "Builtin:Add"


def _fn(jit_name: str, merge: bool = False) -> Function:
    return Function.of(samply.parse_jit_name(jit_name, JIT, None), merge)


def test_hotspots_self_and_total_cpu_time():
    p = _profile(
        (
            "d8",
            1,
            [
                ([MAIN, F_OPT], 1, 3000),
                ([MAIN, F_OPT, BUILTIN], 1, 1000),
                ([MAIN, G], 1, 1000),
            ],
        )
    )
    h = samply.hotspots(p)
    assert h.total_ms == 5.0
    rows = {r.function: (r.self_ms, r.total_ms) for r in h.rows}
    assert rows[_fn(F_OPT)] == (3.0, 4.0)
    assert rows[_fn(MAIN)] == (0.0, 5.0)
    assert rows[_fn(BUILTIN)] == (1.0, 1.0)
    assert [r.function for r in h.rows][:2] == [_fn(F_OPT), _fn(BUILTIN)]


def test_recursion_counts_once_in_total():
    p = _profile(("d8", 1, [([MAIN, F_OPT, F_OPT, F_OPT], 1, 2000)]))
    rows = {r.function: r for r in samply.hotspots(p).rows}
    assert (rows[_fn(F_OPT)].self_ms, rows[_fn(F_OPT)].total_ms) == (2.0, 2.0)


def test_wall_time_counts_blocked_samples():
    p = _profile(("d8", 1, [([MAIN, F_OPT], 1, 1000), ([MAIN, G], 1000, 0)]))
    cpu = {r.function: r.self_ms for r in samply.hotspots(p).rows}
    wall = {r.function: r.self_ms for r in samply.hotspots(p, wall=True).rows}
    assert _fn(G) not in cpu and wall[_fn(G)] == 1000.0
    assert samply.hotspots(p, wall=True).total_ms == 1001.0


def test_tiers_are_separate_unless_merged():
    p = _profile(("d8", 1, [([MAIN, F_OPT], 1, 1000), ([MAIN, F_MAG], 1, 1000)]))
    split = {r.function: r.self_ms for r in samply.hotspots(p).rows if r.self_ms}
    assert split == {_fn(F_OPT): 1.0, _fn(F_MAG): 1.0}
    merged = {
        r.function: r.self_ms
        for r in samply.hotspots(p, merge_tiers=True).rows
        if r.self_ms
    }
    assert merged == {_fn(F_OPT, merge=True): 2.0}


def test_hotspots_by_lib_and_filter():
    p = _profile(("d8", 1, [([MAIN, F_OPT], 1, 1000)]))
    (row,) = samply.hotspots(p, by_lib=True).rows
    assert (row.function.name, row.self_ms) == (JIT, 1.0)
    assert [r.function.name for r in samply.hotspots(p, filter="f a").rows] == [
        "f a.js:2:1"
    ]


def test_callers_paths_from_the_outermost_match():
    p = _profile(
        (
            "d8",
            1,
            [
                ([MAIN, G, F_OPT], 1, 3000),
                ([MAIN, F_OPT, F_OPT], 1, 1000),
                ([MAIN, G], 1, 5000),
            ],
        )
    )
    c = samply.callers(p, "f a.js", depth=2)
    assert c.matched == {"f a.js:2:1"}
    assert (c.total_ms, c.inclusive_ms) == (9.0, 4.0)
    assert [([f.name for f in x.callers], x.ms) for x in c.paths] == [
        (["g a.js:3:1", "main a.js:1:1"], 3.0),
        (["main a.js:1:1"], 1.0),
    ]


def test_symbol_matching_prefers_an_exact_name():
    p = _profile(
        ("d8", 1, [([MAIN, "JS:*f a.js:2:1"], 1, 1), ([MAIN, "JS:*ff a.js:2:1"], 1, 1)])
    )
    t = p.main_thread()
    assert samply.match_functions(p, t, "f a.js:2:1") == {"f a.js:2:1"}
    assert samply.match_functions(p, t, "a.js") == {
        "main a.js:1:1",
        "f a.js:2:1",
        "ff a.js:2:1",
    }
    with pytest.raises(ValueError, match="no function matching 'zzz'"):
        samply.match_functions(p, t, "zzz")


def test_flamegraph_paths_depth_focus_and_cutoff():
    p = _profile(
        (
            "d8",
            1,
            [
                ([MAIN, G, F_OPT, BUILTIN], 1, 6000),
                ([MAIN, G], 1, 3000),
                ([MAIN, F_OPT], 1, 1000),
            ],
        )
    )
    fg = samply.flamegraph(p, depth=2, min_pct=0)
    assert [([f.name for f in x.frames], x.ms) for x in fg.paths] == [
        (["main a.js:1:1", "g a.js:3:1"], 9.0),
        (["main a.js:1:1", "f a.js:2:1"], 1.0),
    ]
    focused = samply.flamegraph(p, focus="f a.js", min_pct=0)
    assert [([f.name for f in x.frames], x.ms) for x in focused.paths] == [
        (["f a.js:2:1", "Add"], 6.0),
        (["f a.js:2:1"], 1.0),
    ]
    assert [x.ms for x in samply.flamegraph(p, min_pct=20).paths] == [6.0, 3.0]


def test_diff_orders_by_change_and_marks_new_and_gone():
    base = _profile(("d8", 1, [([MAIN, F_OPT], 1, 5000), ([MAIN, G], 1, 5000)]))
    after = _profile(("d8", 1, [([MAIN, F_MAG], 1, 9000), ([MAIN, BUILTIN], 1, 1000)]))
    rows = samply.diff(base, after)
    got = [(r.function.name, r.base_pct, r.after_pct) for r in rows]
    # f moved from Turbofan to Maglev but lines up with merged tiers.
    assert got == [
        ("g a.js:3:1", 50.0, None),
        ("f a.js:2:1", 50.0, 90.0),
        ("Add", None, 10.0),
    ]


def test_threads_busiest_first():
    p = _profile(
        ("worker", 2, [([G], 3, 1000)]),
        ("d8", 1, [([MAIN, F_OPT], 2, 2000), ([MAIN], 1, 0)]),
    )
    summary = samply.threads(p)
    assert [(s.thread.name, s.wall_ms, s.cpu_ms) for s in summary] == [
        ("d8", 3.0, 2.0),
        ("worker", 3.0, 1.0),
    ]
    assert summary[0].top[0].function == _fn(F_OPT)


def test_thread_selection():
    p = _profile(("worker", 2, [([G], 1, 5000)]), ("d8", 1, [([MAIN, F_OPT], 1, 1000)]))
    assert samply.hotspots(p).thread.name == "worker"  # most CPU
    assert samply.hotspots(p, "d8").rows[0].function == _fn(F_OPT)
    assert samply.hotspots(p, 1).rows[0].function == _fn(F_OPT)


def test_diff_takes_thread_names():
    p = _profile(("d8", 1, [([MAIN, F_OPT], 1, 1000)]))
    assert samply.diff(p, p, "d8")
    with pytest.raises(TypeError, match="tids differ between recordings"):
        samply.diff(p, p, 1)
