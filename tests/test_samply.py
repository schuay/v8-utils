"""samply profile loading, against a recorded fixture (tests/fixtures/samply)."""

import gzip
import json
from collections import Counter
from pathlib import Path

import pytest

from v8_utils.samply import Profile, parse_jit_name

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "samply"


@pytest.fixture(scope="module")
def profile() -> Profile:
    return Profile.load(FIXTURE_DIR)


def _raw() -> dict:
    return json.loads(gzip.decompress((FIXTURE_DIR / "profile.json.gz").read_bytes()))


@pytest.mark.parametrize(
    ("name", "kind", "tier", "osr", "specialized", "short"),
    [
        ("JS:~foo a.js:1:2", "js", "ignition", False, False, "foo a.js:1:2"),
        ("JS:^foo a.js:1:2", "js", "sparkplug", False, False, "foo a.js:1:2"),
        ("JS:+foo a.js:1:2", "js", "maglev", False, False, "foo a.js:1:2"),
        ("JS:+'foo a.js:1:2", "js", "maglev", False, True, "foo a.js:1:2"),
        ("JS:o+ a.js:1:1", "js", "maglev", True, False, "a.js:1:1"),
        ("JS:*'foo a.js:1:2", "js", "turbofan", False, True, "foo a.js:1:2"),
        ("JS:o* a.js:1:1", "js", "turbofan", True, False, "a.js:1:1"),
        # A function whose name starts with "o" is not OSR code.
        ("JS:~obj a.js:1:2", "js", "ignition", False, False, "obj a.js:1:2"),
        ("JS:ofoo a.js:1:2", "js", "compiled", False, False, "ofoo a.js:1:2"),
        ("Script:~ a.js:1:1", "js", "ignition", False, False, "a.js:1:1"),
        ("Eval:~ :1:1", "js", "ignition", False, False, ":1:1"),
        ("Builtin:JSEntry", "builtin", None, False, False, "JSEntry"),
        ("BytecodeHandler:Add", "builtin", None, False, False, "Add"),
        (
            "RegExp.< src: 'a+' flags: 'g'",
            "regexp",
            None,
            False,
            False,
            "RegExp.< src: 'a+' flags: 'g'",
        ),
        ("Callback:f", "jit", None, False, False, "Callback:f"),
        ("untagged", "jit", None, False, False, "untagged"),
    ],
)
def test_parse_jit_name(name, kind, tier, osr, specialized, short):
    f = parse_jit_name(name, "jit-1.dump", 0x10)
    assert (f.kind, f.tier, f.osr, f.specialized, f.name) == (
        kind,
        tier,
        osr,
        specialized,
        short,
    )
    assert (f.lib, f.address) == ("jit-1.dump", 0x10)


def test_main_thread_is_the_d8_isolate_thread(profile):
    main = profile.main_thread()
    assert (main.name, main.is_main, len(main)) == ("d8", True, 430)
    assert profile.interval_ms == 1.0


def test_hot_leaf_is_the_turbofan_loop(profile):
    leafs = Counter()
    for s in profile.main_thread().samples():
        f = s.frames[0]
        leafs[(f.kind, f.tier, f.osr, f.name)] += s.weight
    assert leafs.most_common(2) == [
        (("js", "turbofan", True, "/work/work.js:1:1"), 183),
        (("js", "turbofan", False, "hotLoop /work/work.js:3:17"), 121),
    ]


def test_stacks_run_from_leaf_to_root(profile):
    s = next(
        s
        for s in profile.main_thread().samples()
        if s.frames[0].name.startswith("hotLoop")
    )
    names = [f.name for f in s.frames]
    # hotLoop is called from the OSR'd top-level loop, entered via JSEntry.
    assert names.index("/work/work.js:1:1") > 0
    assert names.index("JSEntry") > names.index("/work/work.js:1:1")
    assert s.frames[-1].lib == "dyld"


def test_samply_js_label_frames_are_dropped(profile):
    raw = _raw()
    label_frames = sum(
        1
        for t in raw["threads"]
        for i in range(t["frameTable"]["length"])
        if t["frameTable"]["address"][i] == -1
        and t["funcTable"]["isJS"][t["frameTable"]["func"][i]]
    )
    assert label_frames > 0
    for t in profile.threads:
        for s in t.samples():
            assert all(f.address != -1 for f in s.frames)


def test_unsymbolized_natives_are_named_by_lib_and_rva():
    profile = Profile.load(FIXTURE_DIR, symbolize=False)
    natives = {
        f
        for s in profile.main_thread().samples()
        for f in s.frames
        if f.kind == "native"
    }
    assert natives
    for f in natives:
        assert f.name == f"{f.lib}+{f.address:#x}"
    assert profile.unsymbolized == {}


def test_missing_binary_is_reported(profile):
    # The fixture's d8 path was rewritten to one that does not exist.
    assert profile.unsymbolized["d8"] == "/v8/out/d8 not found"
    d8_frames = {
        f.name
        for s in profile.main_thread().samples()
        for f in s.frames
        if f.lib == "d8"
    }
    assert d8_frames and all(n.startswith("d8+0x") for n in d8_frames)


def test_jit_frames_come_from_the_jitdump(profile):
    jit = {
        f.lib
        for s in profile.main_thread().samples()
        for f in s.frames
        if f.kind in ("js", "builtin", "regexp")
    }
    assert len(jit) == 1 and jit.pop().startswith("jit-")


def test_cpu_time_and_weights(profile):
    main = profile.main_thread()
    samples = list(main.samples())
    assert sum(s.weight for s in samples) == len(main)
    assert main.cpu_us() == sum(s.cpu_us for s in samples) > 0


def test_thread_lookup(profile):
    main = profile.main_thread()
    assert profile.thread("d8") is main
    assert profile.thread(main.tid) is main
    with pytest.raises(ValueError, match="ambiguous thread 'V8 DefaultWorke'"):
        profile.thread("V8 DefaultWorke")
    with pytest.raises(ValueError, match="no thread 'nope'"):
        profile.thread("nope")


def test_load_accepts_the_profile_file(tmp_path):
    p = Profile.load(FIXTURE_DIR / "profile.json.gz")
    assert len(p.main_thread()) == 430
    plain = tmp_path / "profile.json"
    plain.write_text(json.dumps(_raw()))
    assert len(Profile.load(plain).main_thread()) == 430


def _write(tmp_path, raw):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(raw))
    return path


def test_unknown_format_version_is_refused(tmp_path):
    raw = _raw()
    raw["meta"]["preprocessedProfileVersion"] = 50
    with pytest.raises(ValueError, match="format version 50 is not supported"):
        Profile.load(_write(tmp_path, raw))


def test_non_sample_weights_are_refused(tmp_path):
    raw = _raw()
    raw["threads"][0]["samples"]["weightType"] = "tracing-ms"
    with pytest.raises(ValueError, match="unsupported weightType 'tracing-ms'"):
        Profile.load(_write(tmp_path, raw))


def test_first_sample_cpu_delta_is_not_counted(profile):
    # samply gives the first sample the thread's CPU time from before
    # sampling started: 5958 us of startup on the fixture's d8 thread.
    raw = _raw()["threads"][0]["samples"]["threadCPUDelta"]
    assert raw[0] == 5958
    main = profile.main_thread()
    assert next(iter(main.samples())).cpu_us == 0
    assert main.cpu_us() == sum(raw[1:])
