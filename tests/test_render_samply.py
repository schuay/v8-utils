"""Name and tier formatting for the samply_* tables."""

import pytest

from v8_utils.api.samply import Function
from v8_utils.render.samply import short_name, tier_label


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
