"""Inline frames and lines from a dSYM in samply analyses, on a binary
compiled by the test."""

import json
import subprocess
import sys

import pytest
from samply_profiles import raw_profile

from v8_utils import disasm, dwarf, macho, samply
from v8_utils.symbols import read_symbols

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Mach-O")

SOURCE = """\
static inline __attribute__((always_inline)) int sq(int x) {
  return x * x;
}
__attribute__((noinline)) int f(int x) {
  return sq(x) + 3;
}
int main(int argc, char **argv) { return f(argc); }
"""


@pytest.fixture(scope="module")
def symbolizer():
    found = samply.find_symbolizer()
    if found is None:
        pytest.skip("no llvm-symbolizer")
    return found


@pytest.fixture(scope="module")
def build(tmp_path_factory, symbolizer):
    """The binary, its image, and an RVA in f whose chain is sq <- f."""
    tmp = tmp_path_factory.mktemp("inl")
    (tmp / "t.c").write_text(SOURCE)
    for cmd in (
        ["cc", "-O0", "-g", "-c", "t.c", "-o", "t.o"],
        ["cc", "t.o", "-o", "t"],
        ["dsymutil", "t"],
    ):
        subprocess.run(cmd, check=True, cwd=tmp, capture_output=True)
    binary = tmp / "t"
    (image,) = macho.images(binary)
    (f,) = [s for s in read_symbols(binary) if s.name == "_f"]
    insns = disasm.disassemble_range(
        binary, f.address, f.address + f.size, disasm.find_objdump()
    )
    dsym = tmp / "t.dSYM/Contents/Resources/DWARF/t"
    chains = dwarf.symbolize(symbolizer, dsym, [i.address for i in insns])
    in_sq = next(a for a, c in chains.items() if len(c) == 2)
    return binary, image, in_sq - image.text_vmaddr


def _profile(tmp_path, build, *samples):
    binary, image, _ = build
    raw = raw_profile(
        ("t", 1, list(samples)),
        libs=[{"name": "t", "path": str(binary), "codeId": image.uuid}],
    )
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(raw))
    return samply.Profile.load(path)


def test_native_frame_becomes_its_inline_chain(tmp_path, build):
    binary, _, rva = build
    p = _profile(tmp_path, build, ([("t", 0, rva)], 1, 2000))
    (sample,) = [s for s in p.main_thread().samples() if s.frames]
    sq, f = sample.frames
    src = str(binary.parent / "t.c")
    assert (sq.name, sq.inlined, sq.source, sq.defined_at) == (
        "sq",
        True,
        (src, 2),
        (src, 1),
    )
    assert (f.name, f.inlined, f.source, f.defined_at) == (
        "f",
        False,
        (src, 5),
        (src, 4),
    )
    assert sq.address == f.address == rva
    assert p.no_lines == {}


def test_analyses_see_inlined_functions(tmp_path, build, symbolizer):
    _, _, rva = build
    p = _profile(tmp_path, build, ([("t", 0, rva)], 1, 2000))
    rows = {r.function.name: (r.self_ms, r.total_ms) for r in samply.hotspots(p).rows}
    assert rows == {"sq": (2.0, 2.0), "f": (0.0, 2.0)}

    c = samply.callers(p, "sq")
    ((path),) = c.paths
    assert [fn.name for fn in path.callers] == ["f"]
    assert path.sources[0][1] == 5

    (flame,) = samply.flamegraph(p, min_pct=0, native_root=True).paths
    assert [fn.name for fn in flame.frames] == ["f", "sq"]
    assert flame.inlined == (False, True)

    # The sample's leaf is the inlined sq, but it ran in f's code.
    objdump = disasm.find_objdump()
    for symbol in ("f", "sq"):
        a = samply.annotate(p, symbol, objdump=objdump)
        assert [o.label for o in a.objects] == ["t f"]
        assert sum(line.ms for line in a.lines) == 2.0


def test_no_dsym_means_names_only(tmp_path, build):
    binary, image, rva = build
    plain = tmp_path / "plain"
    plain.write_bytes(binary.read_bytes())
    raw = raw_profile(
        ("t", 1, [([("t", 0, rva)], 1, 2000)]),
        libs=[{"name": "t", "path": str(plain), "codeId": image.uuid}],
    )
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(raw))
    p = samply.Profile.load(path)
    (sample,) = [s for s in p.main_thread().samples() if s.frames]
    assert [(f.name, f.inlined, f.source) for f in sample.frames] == [
        ("f", False, None)
    ]
    assert p.no_lines == {"t": "no dSYM matching the recording"}


def test_missing_symbolizer_is_reported(tmp_path, build, monkeypatch):
    monkeypatch.setattr(samply, "find_symbolizer", lambda: None)
    _, _, rva = build
    p = _profile(tmp_path, build, ([("t", 0, rva)], 1, 2000))
    assert "llvm-symbolizer not found" in p.no_lines["t"]
    (sample,) = [s for s in p.main_thread().samples() if s.frames]
    assert [f.name for f in sample.frames] == ["f"]
