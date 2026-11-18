"""Native symbolization of samply frames, on binaries compiled by the test."""

import subprocess
import sys

import pytest

from v8_utils import macho
from v8_utils.samply import lib_symbols
from v8_utils.symbols import read_symbols

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="needs Xcode tools")

SOURCE = """\
int myfunc(int x) { return x * 3 + 1; }
int other(int x) { return myfunc(x) - 2; }
int main(void) { return other(1); }
"""


def _build(tmp_path, *flags: str):
    src = tmp_path / "t.c"
    src.write_text(SOURCE)
    out = tmp_path / "t"
    r = subprocess.run(
        ["cc", "-O1", *flags, "-o", str(out), str(src)],
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        pytest.skip(f"cc {' '.join(flags)} failed")
    return out


def _rva(image: macho.Image, symbols, name: str) -> int:
    (sym,) = [s for s in symbols if s.name == f"_{name}"]
    return sym.address - image.text_vmaddr


def test_thin_binary(tmp_path):
    binary = _build(tmp_path)
    (image,) = macho.images(binary)
    rva = _rva(image, read_symbols(binary), "myfunc")
    table = lib_symbols(binary, image.uuid)
    # A caller frame's address is inside the function, not at its start.
    assert table.lookup(rva + 4).name == "myfunc"
    assert table.lookup(_rva(image, read_symbols(binary), "main")).name == "main"


def test_universal_binary_uses_the_recorded_slice(tmp_path):
    binary = _build(tmp_path, "-arch", "arm64", "-arch", "x86_64")
    slices = macho.images(binary)
    assert len(slices) == 2
    data = binary.read_bytes()
    for image in slices:
        thin = tmp_path / f"slice-{image.uuid}"
        thin.write_bytes(data[image.offset : image.offset + image.size])
        rva = _rva(image, read_symbols(thin), "other")
        assert lib_symbols(binary, image.uuid).lookup(rva).name == "other"


def test_stripped_binary_uses_its_dsym(tmp_path):
    binary = _build(tmp_path, "-g")
    (image,) = macho.images(binary)
    rva = _rva(image, read_symbols(binary), "myfunc")
    subprocess.run(["dsymutil", str(binary)], check=True, capture_output=True)
    subprocess.run(["strip", str(binary)], check=True, capture_output=True)
    assert not any(s.name == "_myfunc" for s in read_symbols(binary))
    assert lib_symbols(binary, image.uuid).lookup(rva).name == "myfunc"


def test_rebuilt_binary_is_not_trusted(tmp_path):
    binary = _build(tmp_path)
    reason = lib_symbols(binary, "0" * 32)
    assert isinstance(reason, str) and "does not match the recording" in reason


def test_missing_file(tmp_path):
    assert lib_symbols(tmp_path / "gone", "0" * 32) == f"{tmp_path / 'gone'} not found"
