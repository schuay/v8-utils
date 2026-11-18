"""Mach-O image headers, checked against dwarfdump on compiled binaries."""

import re
import subprocess
import sys

import pytest

from v8_utils.macho import images

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="needs Xcode tools")


def _build(tmp_path, *arch_flags: str):
    src = tmp_path / "t.c"
    src.write_text("int main(void) { return 0; }\n")
    out = tmp_path / "t"
    r = subprocess.run(
        ["cc", *arch_flags, "-o", str(out), str(src)], capture_output=True, check=False
    )
    if r.returncode != 0:
        pytest.skip(f"cc {' '.join(arch_flags)} failed")
    return out


def _dwarfdump_uuids(path) -> list[str]:
    out = subprocess.run(
        ["dwarfdump", "--uuid", str(path)], capture_output=True, text=True, check=True
    ).stdout
    return [u.replace("-", "") for u in re.findall(r"UUID: ([0-9A-F-]+)", out)]


def test_thin_binary(tmp_path):
    binary = _build(tmp_path)
    (image,) = images(binary)
    assert [image.uuid] == _dwarfdump_uuids(binary)
    assert image.text_vmaddr == 0x100000000
    assert (image.offset, image.size, image.universal) == (
        0,
        binary.stat().st_size,
        False,
    )


def test_universal_binary(tmp_path):
    binary = _build(tmp_path, "-arch", "arm64", "-arch", "x86_64")
    found = images(binary)
    assert [i.uuid for i in found] == _dwarfdump_uuids(binary)
    assert all(i.universal and i.offset > 0 for i in found)
    # Each slice is a complete Mach-O file on its own.
    data = binary.read_bytes()
    for i in found:
        thin = tmp_path / f"slice-{i.uuid}"
        thin.write_bytes(data[i.offset : i.offset + i.size])
        assert [x.uuid for x in images(thin)] == [i.uuid]


def test_other_files_have_no_images(tmp_path):
    (tmp_path / "text").write_text("not a binary\n")
    (tmp_path / "empty").write_bytes(b"")
    assert images(tmp_path / "text") == images(tmp_path / "empty") == []
