"""Disassembly with llvm-objdump, of JIT code bytes and of native code in
Mach-O images.

llvm-objdump only disassembles object files, so JIT code is wrapped in a
minimal ELF file whose .text sits at the code's runtime address; the
listing then carries runtime addresses. Instructions are read from the
address column of `llvm-objdump -d --no-show-raw-insn` lines
("<hex addr>:<tab><instruction>").
"""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_LINE_RE = re.compile(r"^\s*([0-9a-f]+):\s+(\S.*)$")


@dataclass(frozen=True)
class Instruction:
    address: int
    text: str


def find_objdump(v8_root: Path | None = None) -> str:
    """Xcode's llvm-objdump, else V8's bundled one, else one on PATH."""
    xcrun = shutil.which("xcrun")
    if xcrun:
        r = subprocess.run(
            [xcrun, "-f", "llvm-objdump"], capture_output=True, text=True, check=False
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    if v8_root is not None:
        bundled = v8_root / "third_party/llvm-build/Release+Asserts/bin/llvm-objdump"
        if bundled.is_file():
            return str(bundled)
    found = shutil.which("llvm-objdump")
    if found:
        return found
    raise FileNotFoundError(
        "llvm-objdump not found. Install the Xcode command line tools"
        " (`xcode-select --install`) or LLVM, or build V8 once so that"
        " third_party/llvm-build has it."
    )


def disassemble_code(
    code: bytes, address: int, machine: int, objdump: str
) -> list[Instruction]:
    """Instructions of raw `code` loaded at `address`, for ELF e_machine
    `machine`."""
    with tempfile.NamedTemporaryFile(suffix=".o") as f:
        f.write(_elf(code, address, machine))
        f.flush()
        return _run(objdump, [f.name])


def disassemble_range(
    path: Path, start: int, stop: int, objdump: str
) -> list[Instruction]:
    """Instructions of `path` in [start, stop), at link-time addresses."""
    return _run(
        objdump, [f"--start-address={start:#x}", f"--stop-address={stop:#x}", str(path)]
    )


def _run(objdump: str, args: list[str]) -> list[Instruction]:
    r = subprocess.run(
        [objdump, "-d", "--no-show-raw-insn", "--print-imm-hex", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"llvm-objdump failed: {r.stderr.strip()[:500]}")
    out = []
    for line in r.stdout.splitlines():
        m = _LINE_RE.match(line)
        if m:
            out.append(Instruction(int(m.group(1), 16), " ".join(m.group(2).split())))
    return out


def _elf(code: bytes, address: int, machine: int) -> bytes:
    """An ELF64 relocatable file holding `code` as .text at `address`."""
    shstrtab = b"\0.text\0.shstrtab\0"
    header_size = section_header_size = 64
    code_offset = header_size
    shstrtab_offset = code_offset + len(code)
    sections_offset = (shstrtab_offset + len(shstrtab) + 7) & ~7
    header = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9)  # 64-bit, little endian
    header += struct.pack(
        "<HHIQQQIHHHHHH",
        1,  # ET_REL
        machine,
        1,  # EV_CURRENT
        0,
        0,
        sections_offset,
        0,
        header_size,
        0,
        0,
        section_header_size,
        3,  # sections: null, .text, .shstrtab
        2,  # index of .shstrtab
    )
    section = struct.Struct("<IIQQQQIIQQ")
    text = section.pack(1, 1, 0x6, address, code_offset, len(code), 0, 0, 4, 0)
    names = section.pack(7, 3, 0, 0, shstrtab_offset, len(shstrtab), 0, 0, 1, 0)
    out = header + code + shstrtab
    return (
        out
        + bytes(sections_offset - len(out))
        + bytes(section_header_size)
        + text
        + names
    )
