"""jitdump reading, on files written the way V8's perf-jit.cc writes them."""

import struct

import pytest

from v8_utils.jitdump import EM_AARCH64, JitDump


def _record(kind: int, body: bytes, timestamp: int = 0) -> bytes:
    return struct.pack("<IIQ", kind, 16 + len(body), timestamp) + body


def _load(addr: int, code: bytes, name: str, timestamp: int = 0) -> bytes:
    body = struct.pack("<IIQQQQ", 1, 1, addr, addr, len(code), 0)
    return _record(0, body + name.encode() + b"\0" + code, timestamp)


def _debug(addr: int, entries: list[tuple[int, int, int, str]]) -> bytes:
    body = struct.pack("<QQ", addr, len(entries))
    for offset, line, column, script in entries:
        # perf-jit.cc adds kElfHeaderSize (0x34) to each entry's address.
        body += struct.pack("<Qii", addr + offset + 0x34, line, column)
        body += script.encode() + b"\0"
    body += bytes(-len(body) % 8)
    return _record(2, body)


def _write(tmp_path, *records: bytes):
    header = struct.pack("<IIIIIIQQ", 0x4A695444, 1, 40, EM_AARCH64, 0, 1, 0, 0)
    path = tmp_path / "jit-1.dump"
    path.write_bytes(header + b"".join(records))
    return path


def test_loads_positions_and_offsets(tmp_path):
    a = bytes(range(16))
    b = bytes(8)
    path = _write(
        tmp_path,
        _debug(0x1000, [(8, 3, 5, "app.js"), (0, 2, 1, "app.js")]),
        _load(0x1000, a, "JS:*f app.js:2:1", timestamp=7),
        _record(4, bytes(24)),  # unwinding info, skipped
        _load(0x2000, b, "Builtin:Add"),
    )
    jd = JitDump.read(path)
    assert jd.machine == EM_AARCH64
    f, add = jd.loads
    assert (f.name, f.code_addr, f.code, f.timestamp, f.start) == (
        "JS:*f app.js:2:1",
        0x1000,
        a,
        7,
        0,
    )
    assert (add.name, add.start, add.positions) == ("Builtin:Add", 16, ())
    assert [(p.offset, p.line, p.column) for p in f.positions] == [(0, 2, 1), (8, 3, 5)]
    assert f.position_at(4).line == 2 and f.position_at(9).line == 3

    assert jd.load_at(0) is f and jd.load_at(15) is f
    assert jd.load_at(16) is add and jd.load_at(23) is add
    assert jd.load_at(24) is None


def test_positions_attach_to_their_own_code(tmp_path):
    path = _write(
        tmp_path,
        _load(0x1000, bytes(4), "Builtin:A"),
        _debug(0x2000, [(0, 9, 1, "b.js")]),
        _load(0x2000, bytes(4), "JS:~g b.js:9:1"),
    )
    a, g = JitDump.read(path).loads
    assert a.positions == ()
    assert [p.line for p in g.positions] == [9]


def test_not_a_jitdump(tmp_path):
    path = tmp_path / "x.dump"
    path.write_bytes(bytes(64))
    with pytest.raises(ValueError, match="is not a jitdump"):
        JitDump.read(path)
