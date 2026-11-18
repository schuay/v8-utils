"""V8's jitdump (--perf-prof): JIT code bytes and source positions.

Format: the Linux perf jitdump specification, as written by
src/diagnostics/perf-jit.cc. A debug-info record precedes the code-load
record of the code it describes.
"""

from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass
from pathlib import Path

_MAGIC = 0x4A695444
_RECORD = struct.Struct("<IIQ")  # id, total size, timestamp
_CODE_LOAD = struct.Struct("<IIQQQQ")  # pid, tid, vma, code_addr, code_size, index
_DEBUG_INFO = struct.Struct("<QQ")  # code_addr, entry count
_DEBUG_ENTRY = struct.Struct("<Qii")  # address, line, column; then a name
_LOAD, _DEBUG = 0, 2

# ELF e_machine values from the jitdump header.
EM_386 = 0x03
EM_ARM = 0x28
EM_X86_64 = 0x3E
EM_AARCH64 = 0xB7


def _elf_header_size(machine: int) -> int:
    """perf-jit.cc offsets debug-entry addresses by the ELF header size of
    the target (PerfJitLogger::kElfHeaderSize) for `perf inject`, which
    places the code after one. RISC-V names both widths alike; take the
    64-bit one."""
    return 0x34 if machine in (EM_386, EM_ARM) else 0x40


@dataclass(frozen=True)
class SourcePosition:
    offset: int  # into the code
    line: int  # 1-based
    column: int  # 1-based
    script: str


@dataclass(frozen=True)
class CodeLoad:
    name: str
    code_addr: int
    code: bytes
    timestamp: int
    # Where this code starts in samply's view of the jitdump as one library:
    # every load's code bytes concatenated in file order.
    start: int
    positions: tuple[SourcePosition, ...]

    def position_at(self, offset: int) -> SourcePosition | None:
        """The source position whose code range holds `offset`."""
        i = bisect.bisect_right([p.offset for p in self.positions], offset) - 1
        return self.positions[i] if i >= 0 else None


class JitDump:
    def __init__(self, machine: int, loads: list[CodeLoad]) -> None:
        self.machine = machine
        self.loads = loads
        self._starts = [load.start for load in loads]
        # By runtime address; for reused addresses the later load wins.
        self._by_address = sorted(
            range(len(loads)), key=lambda i: (loads[i].code_addr, i)
        )
        self._addresses = [loads[i].code_addr for i in self._by_address]

    @classmethod
    def read(cls, path: Path) -> JitDump:
        data = path.read_bytes()
        magic, _, header_size, machine = struct.unpack_from("<IIII", data)
        if magic != _MAGIC:
            raise ValueError(f"{path} is not a jitdump")
        loads: list[CodeLoad] = []
        positions: dict[int, tuple[SourcePosition, ...]] = {}
        start = 0
        pos = header_size
        while pos + _RECORD.size <= len(data):
            kind, size, timestamp = _RECORD.unpack_from(data, pos)
            if size < _RECORD.size:
                raise ValueError(f"{path}: bad record size {size} at {pos:#x}")
            body = pos + _RECORD.size
            if kind == _LOAD:
                _, _, _, code_addr, code_size, _ = _CODE_LOAD.unpack_from(data, body)
                name_start = body + _CODE_LOAD.size
                name_end = data.index(b"\0", name_start)
                code = data[name_end + 1 : name_end + 1 + code_size]
                loads.append(
                    CodeLoad(
                        data[name_start:name_end].decode(errors="replace"),
                        code_addr,
                        code,
                        timestamp,
                        start,
                        positions.pop(code_addr, ()),
                    )
                )
                start += code_size
            elif kind == _DEBUG:
                code_addr, count = _DEBUG_INFO.unpack_from(data, body)
                positions[code_addr] = _read_positions(
                    data, body, code_addr, count, _elf_header_size(machine)
                )
            pos += size
        return cls(machine, loads)

    def load_at(self, offset: int) -> CodeLoad | None:
        """The code load holding `offset` in samply's concatenated view."""
        i = bisect.bisect_right(self._starts, offset) - 1
        if i < 0:
            return None
        load = self.loads[i]
        return load if offset < load.start + len(load.code) else None

    def load_containing(self, address: int) -> CodeLoad | None:
        """The code load whose code holds runtime `address`."""
        i = bisect.bisect_right(self._addresses, address) - 1
        if i < 0:
            return None
        load = self.loads[self._by_address[i]]
        return load if address < load.code_addr + len(load.code) else None


def _read_positions(
    data: bytes, body: int, code_addr: int, count: int, bias: int
) -> tuple[SourcePosition, ...]:
    out = []
    pos = body + _DEBUG_INFO.size
    for _ in range(count):
        address, line, column = _DEBUG_ENTRY.unpack_from(data, pos)
        name_start = pos + _DEBUG_ENTRY.size
        name_end = data.index(b"\0", name_start)
        out.append(
            SourcePosition(
                address - bias - code_addr,
                line,
                column,
                data[name_start:name_end].decode(errors="replace"),
            )
        )
        pos = name_end + 1
    # The source position table is in code order, but keep lookups correct
    # if a writer ever emits it otherwise.
    return tuple(sorted(out, key=lambda p: p.offset))
