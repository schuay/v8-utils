"""llvm-objdump disassembly of raw code bytes and of native ranges."""

import subprocess
import sys

import pytest

from v8_utils import disasm, macho
from v8_utils.jitdump import EM_AARCH64, EM_X86_64
from v8_utils.symbols import read_symbols


@pytest.fixture(scope="module")
def objdump():
    try:
        return disasm.find_objdump()
    except FileNotFoundError:
        pytest.skip("no llvm-objdump")


def test_arm64_code_at_its_runtime_address(objdump):
    # add x0, x1, x2; mov x7, x0; ret
    code = bytes.fromhex("2000028be70300aac0035fd6")
    insns = disasm.disassemble_code(code, 0x170001040, EM_AARCH64, objdump)
    assert [(i.address, i.text) for i in insns] == [
        (0x170001040, "add x0, x1, x2"),
        (0x170001044, "mov x7, x0"),
        (0x170001048, "ret"),
    ]


def test_x64_code(objdump):
    # mov rax, rcx; ret
    insns = disasm.disassemble_code(
        bytes.fromhex("4889c8c3"), 0x5000, EM_X86_64, objdump
    )
    assert [i.address for i in insns] == [0x5000, 0x5003]
    assert insns[-1].text.startswith("ret")


@pytest.mark.skipif(sys.platform != "darwin", reason="Mach-O")
def test_native_range(objdump, tmp_path):
    src = tmp_path / "t.c"
    src.write_text(
        "int f(int x) { return x * 3 + 1; }\nint main(void) { return f(1); }\n"
    )
    binary = tmp_path / "t"
    subprocess.run(["cc", "-O1", "-o", str(binary), str(src)], check=True)
    (sym,) = [s for s in read_symbols(binary) if s.name == "_f"]
    insns = disasm.disassemble_range(
        binary, sym.address, sym.address + sym.size, objdump
    )
    assert insns[0].address == sym.address
    assert all(sym.address <= i.address < sym.address + sym.size for i in insns)
    assert any(i.text.startswith("ret") for i in insns)
    assert macho.images(binary)[0].text_vmaddr <= sym.address


def test_missing_objdump_names_the_install(monkeypatch):
    monkeypatch.setattr(disasm.shutil, "which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="xcode-select --install"):
        disasm.find_objdump()
