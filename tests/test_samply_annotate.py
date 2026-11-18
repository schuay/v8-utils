"""samply_annotate's core, on a synthetic recording with real code bytes."""

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from samply_profiles import JIT, raw_profile

from v8_utils import disasm, macho, samply
from v8_utils.jitdump import EM_AARCH64
from v8_utils.symbols import read_symbols

CALLEE = "Builtin:Callee"
F_OPT = "JS:*f a.js:2:1"
F_MAG = "JS:+f a.js:2:1"
MAIN = "JS:~main a.js:1:1"

RET = bytes.fromhex("c0035fd6")
ADD = bytes.fromhex("2000028b")  # add x0, x1, x2


def _bl(source: int, target: int) -> bytes:
    return struct.pack("<I", 0x94000000 | ((target - source) // 4 & 0x3FFFFFF))


@pytest.fixture(scope="module")
def objdump():
    try:
        return disasm.find_objdump()
    except FileNotFoundError:
        pytest.skip("no llvm-objdump")


def _jitdump(path: Path) -> None:
    def record(kind, body):
        return struct.pack("<IIQ", kind, 16 + len(body), 0) + body

    def load(addr, code, name):
        head = struct.pack("<IIQQQQ", 1, 1, addr, addr, len(code), 0)
        return record(0, head + name.encode() + b"\0" + code)

    def debug(addr, entries):
        body = struct.pack("<QQ", addr, len(entries))
        for offset, line, column in entries:
            # V8 adds the arm64 ELF header size (0x40) to each entry address.
            body += struct.pack("<Qii", addr + offset + 0x40, line, column) + b"a.js\0"
        return record(2, body + bytes(-len(body) % 8))

    f_opt = ADD + _bl(0x1004, 0x9000) + RET
    header = struct.pack("<IIIIIIQQ", 0x4A695444, 1, 40, EM_AARCH64, 0, 1, 0, 0)
    path.write_bytes(
        header
        + load(0x9000, RET, CALLEE)  # jitdump offsets 0..4
        + debug(0x1000, [(0, 2, 1), (4, 3, 5)])
        + load(0x1000, f_opt, F_OPT)  # 4..16
        + load(0x2000, RET, F_MAG)  # 16..20
    )


@pytest.fixture
def recording(tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "a.js").write_text("main();\nfunction f() {\n  return g();\n}\n")
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "meta.json").write_text(json.dumps({"suite_dir": str(suite)}))
    _jitdump(rec / JIT)
    main = (MAIN, 0, 0)
    raw = raw_profile(
        (
            "d8",
            1,
            [
                ([main, (F_OPT, 0, 4 + 4)], 1, 3000),  # at the bl
                ([main, (F_OPT, 0, 4 + 0)], 1, 1000),  # at the add
                ([main, (F_MAG, 0, 16)], 1, 500),
            ],
        ),
        libs=[{"name": JIT, "path": f"/elsewhere/{JIT}", "codeId": ""}],
    )
    path = rec / "profile.json"
    path.write_text(json.dumps(raw))
    return samply.Profile.load(path, symbolize=False)


def test_jit_listing_with_source_and_heat(recording, objdump):
    a = samply.annotate(recording, "f a.js:2:1", objdump=objdump)
    assert [(o.label, o.ms) for o in a.objects] == [(F_OPT, 4.0), (F_MAG, 0.5)]
    assert a.selected == 0 and a.unplaced_ms == 0
    assert [(line.address, line.text, line.ms) for line in a.lines] == [
        (None, "a.js:2:1  function f() {", 0.0),
        (0x1000, "add x0, x1, x2", 1.0),
        (None, "a.js:3:5  return g();", 0.0),
        # A call into other JIT code is labelled with that code's name.
        (0x1004, f"bl 0x9000 <{CALLEE}>", 3.0),
        (0x1008, "ret", 0.0),
    ]


def test_other_code_objects_by_number(recording, objdump):
    a = samply.annotate(recording, "f a.js:2:1", code=2, objdump=objdump)
    assert a.selected == 1
    assert [(line.address, line.text, line.ms) for line in a.lines] == [
        (0x2000, "ret", 0.5)
    ]
    with pytest.raises(ValueError, match="code must be 1..2"):
        samply.annotate(recording, "f a.js:2:1", code=3, objdump=objdump)


def test_no_leaf_samples(recording, objdump):
    with pytest.raises(ValueError, match="no samples with main a.js:1:1 as the leaf"):
        samply.annotate(recording, "main a.js:1:1", objdump=objdump)


@pytest.mark.skipif(sys.platform != "darwin", reason="Mach-O")
def test_native_listing(tmp_path, objdump):
    src = tmp_path / "t.c"
    src.write_text(
        "__attribute__((noinline)) int f(int x) { return x * 3 + 1; }\n"
        "int main(int argc, char **argv) { return f(argc); }\n"
    )
    binary = tmp_path / "t"
    subprocess.run(["cc", "-O1", "-o", str(binary), str(src)], check=True)
    (image,) = macho.images(binary)
    syms = {s.name: s for s in read_symbols(binary)}
    f, main = syms["_f"], syms["_main"]
    rva = f.address - image.text_vmaddr
    raw = raw_profile(
        (
            "t",
            1,
            [([("main", 0, main.address - image.text_vmaddr), ("f", 0, rva)], 1, 2000)],
        ),
        libs=[{"name": "t", "path": str(binary), "codeId": image.uuid}],
    )
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(raw))
    profile = samply.Profile.load(path)

    a = samply.annotate(profile, "f", objdump=objdump)
    assert [o.label for o in a.objects] == ["t f"]
    assert a.lines[0].address == f.address and a.lines[0].ms == 2.0
    assert sum(line.ms for line in a.lines) == 2.0

    # main's (tail) call into f is labelled from the symbol table.
    raw_main = raw_profile(
        ("t", 1, [([("main", 0, main.address - image.text_vmaddr)], 1, 1000)]),
        libs=[{"name": "t", "path": str(binary), "codeId": image.uuid}],
    )
    path.write_text(json.dumps(raw_main))
    a = samply.annotate(samply.Profile.load(path), "main", objdump=objdump)
    assert any(line.text.endswith(f" {f.address:#x} <f>") for line in a.lines)


@pytest.mark.parametrize(
    ("text", "labelled"),
    [
        ("bl 0x9000", "bl 0x9000 <T>"),
        ("b.ne 0x9004 <.text+0x4>", "b.ne 0x9004 <T+4>"),
        ("ldr w11, 0x9008 <.text+0x8>", "ldr w11, 0x9008 <T+8>"),
        ("add x0, x1, #0x9000", "add x0, x1, #0x9000"),
        ("movl $0x9000, %eax # imm = 0x9000", "movl $0x9000, %eax # imm = 0x9000"),
        ("movq 0x9000(%rax), %rcx", "movq 0x9000(%rax), %rcx"),
        ("callq 0x9000 <.text+0x16>", "callq 0x9000 <T>"),
        ("jmp 0x5 <.text+0x5>", "jmp 0x5"),
    ],
)
def test_relabel_only_address_operands(text, labelled):
    def name_at(address):
        return (
            None
            if address < 0x9000
            else ("T" if address == 0x9000 else f"T+{address - 0x9000}")
        )

    assert samply._relabel(text, name_at) == labelled
