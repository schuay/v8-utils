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


def test_syms_sidecar_symbolizes_missing_binary_and_inlines(tmp_path):
    import json
    from v8_utils.samply import NativeNames, syms_sidecar

    profile_path = tmp_path / "profile.json.gz"
    sidecar_path = syms_sidecar(profile_path)
    assert sidecar_path == tmp_path / "profile.json.syms.json"

    sidecar = {
        "string_table": [
            "Outer()",
            "Inner()",
            "/src/out/release/../../src/inner.h",
            "/src/out/release/../../src/outer.cc",
            "malloc",
        ],
        "data": [
            {
                "debug_name": "d8",
                "code_id": "AABBCCDD00112233445566778899AABB",
                "symbol_table": [
                    {
                        "rva": 0x1000,
                        "size": 0x80,
                        "symbol": 0,
                        "frames": [
                            {"function": 1, "file": 2, "line": 42},
                            {"function": 0, "file": 3, "line": 10},
                        ],
                    }
                ],
                "known_addresses": [[0x1014, 0]],
            },
            {
                "debug_name": "libsystem_malloc.dylib",
                "code_id": "11223344556677889900AABBCCDDEEFF",
                "symbol_table": [{"rva": 0x2000, "size": 0x40, "symbol": 4}],
                "known_addresses": [[0x2008, 0]],
            },
        ],
    }
    sidecar_path.write_text(json.dumps(sidecar))

    libs = [
        {
            "name": "d8",
            "path": str(tmp_path / "rebuilt-or-deleted-d8"),
            "codeId": "aabbccdd00112233445566778899aabb",
        },
        {
            "name": "libsystem_malloc.dylib",
            "path": "/usr/lib/system/libsystem_malloc.dylib",
            "codeId": "11223344556677889900AABBCCDDEEFF",
        },
    ]
    natives = NativeNames(libs, symbolizer=None, syms_path=sidecar_path)
    natives.prepare({0: {0x1014}, 1: {0x2008}})

    assert natives.unavailable() == {}
    assert natives.no_lines == {}

    d8_frames = natives(0, 0x1014)
    assert d8_frames is not None and len(d8_frames) == 2
    assert d8_frames[0].name == "Inner()"
    assert d8_frames[0].source == ("/src/src/inner.h", 42)
    assert d8_frames[0].inlined is True
    assert d8_frames[1].name == "Outer()"
    assert d8_frames[1].source == ("/src/src/outer.cc", 10)
    assert d8_frames[1].inlined is False

    # Range fallback when an RVA isn't listed in known_addresses:
    assert natives(0, 0x1020)[0].name == "Inner()"

    malloc_frames = natives(1, 0x2008)
    assert malloc_frames is not None and len(malloc_frames) == 1
    assert malloc_frames[0].name == "malloc"
    assert malloc_frames[0].source is None
    assert malloc_frames[0].inlined is False
