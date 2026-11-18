"""Name and tier formatting for the samply_* tables."""

from pathlib import Path

import pytest
from samply_profiles import raw_profile

from v8_utils.api.samply import (
    AnnotatedLine,
    Annotation,
    CodeObject,
    Function,
    Profile,
)
from v8_utils.render.samply import (
    format_annotation,
    format_read_around,
    short_name,
    tier_label,
)


@pytest.mark.parametrize(
    ("name", "short"),
    [
        ("v8::internal::Heap::Scavenge(int, bool)", "v8::internal::Heap::Scavenge"),
        (
            "v8::(anonymous namespace)::ProcessMessages(v8::Isolate*)",
            "v8::(anonymous namespace)::ProcessMessages",
        ),
        ("Foo::operator()(int) const", "Foo::operator()"),
        (
            "std::function<int (bool)>::operator()",
            "std::function<int (bool)>::operator()",
        ),
        ("Bar<(anonymous)>::f(int) (.llvm.123)", "Bar<(anonymous)>::f"),
        ("_platform_memmove", "_platform_memmove"),
    ],
)
def test_short_name_drops_cpp_parameters(name, short):
    assert short_name(Function(name, "native", "d8")) == short


def test_short_name_keeps_js_names():
    js = Function("f (app.js):1:2", "js", "jit-1.dump", "turbofan")
    assert short_name(js) == "f (app.js):1:2"


def test_tier_label():
    assert tier_label(Function("f", "js", None, "maglev", osr=True)) == "maglev osr"
    assert tier_label(Function("f", "js", None, "turbofan", specialized=True)) == (
        "turbofan ctx"
    )
    assert tier_label(Function("Add", "builtin", None)) == ""


def _annotation(heat: list[float]):
    f = Function("f a.js:1:1", "js", "jit-1.dump", "turbofan")
    lines = [AnnotatedLine(0x1000 + 4 * i, f"insn{i}", ms) for i, ms in enumerate(heat)]
    profile = Profile(raw_profile(("d8", 1, [])), Path("/rec/p.json"), symbolize=False)
    (thread,) = profile.threads
    obj = CodeObject(f, "JS:*f a.js:1:1", sum(heat))
    return Annotation(thread, False, {f.name}, [obj], 0, lines, 0.0)


def test_hot_blocks_merge_and_order_by_peak():
    heat = [0.0] * 30
    heat[3], heat[5], heat[20] = 10.0, 1.0, 50.0
    text = format_annotation(_annotation(heat), min_pct=1, context=2, top=3)
    blocks = [line for line in text.splitlines() if line.startswith("Hot block")]
    # Lines 4 and 6 are within context of each other and form one block.
    assert blocks == [
        "Hot block #1 (lines 19-23, peak 82.0%):",
        "Hot block #2 (lines 2-8, peak 16.4%):",
    ]
    assert "    21   82.0%          1050  insn20" in text


def test_read_around_bounds():
    a = _annotation([1.0] * 5)
    text = format_read_around(a, 1, context=1)
    assert text.splitlines()[-2:] == [
        "     1   20.0%          1000  insn0",
        "     2   20.0%          1004  insn1",
    ]
    with pytest.raises(ValueError, match="line must be 1..5"):
        format_read_around(a, 6)


def test_location():
    from v8_utils.render.samply import location

    assert location(("/b/src/v8/src/heap/factory.cc", 306)) == "heap/factory.cc:306"
    assert location(None) == ""
