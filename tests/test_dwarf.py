"""Inline chains and lines from a dSYM, on a binary compiled by the test."""

import subprocess
import sys
from pathlib import Path

import pytest

from v8_utils import disasm, dwarf
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
    found = dwarf.find_symbolizer([Path.home() / "src/v8/main"])
    if found is None:
        pytest.skip("no llvm-symbolizer")
    return found


@pytest.fixture(scope="module")
def binary(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dwarf")
    (tmp / "t.c").write_text(SOURCE)
    out = tmp / "t"
    # Compile separately: dsymutil collects DWARF from the object file, which
    # a one-step `cc -g` deletes. At -O1 sq's multiply carries f's line; -O0
    # keeps instructions in sq's inline scope.
    for cmd in (
        ["cc", "-O0", "-g", "-c", "t.c", "-o", "t.o"],
        ["cc", "t.o", "-o", "t"],
        ["dsymutil", "t"],
    ):
        subprocess.run(cmd, check=True, cwd=tmp, capture_output=True)
    return out


def _dsym(binary: Path) -> Path:
    return (
        binary.with_name(binary.name + ".dSYM")
        / "Contents/Resources/DWARF"
        / binary.name
    )


def test_inline_chain_innermost_first(binary, symbolizer):
    (f,) = [s for s in read_symbols(binary) if s.name == "_f"]
    insns = disasm.disassemble_range(
        binary, f.address, f.address + f.size, disasm.find_objdump()
    )
    chains = dwarf.symbolize(symbolizer, _dsym(binary), [i.address for i in insns])
    inlined = [c for c in chains.values() if len(c) == 2]
    assert inlined, chains
    sq, outer = inlined[0]
    assert (sq.function, sq.line, sq.start_line) == ("sq", 2, 1)
    assert (outer.function, outer.line, outer.start_line) == ("f", 5, 4)
    assert outer.file == str(binary.parent / "t.c")


def test_addresses_without_lines(binary, symbolizer):
    assert dwarf.symbolize(symbolizer, _dsym(binary), [0x1]) == {0x1: ()}
    assert dwarf.symbolize(symbolizer, _dsym(binary), []) == {}
