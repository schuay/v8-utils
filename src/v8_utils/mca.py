"""llvm-mca pipeline analysis of assembly copied from V8, perf or gdb output."""

import re as _re
import shutil
import subprocess


_RE_V8_PRINT_CODE = _re.compile(r"^0x[0-9a-f]+\s+[0-9a-f]+\s+[0-9a-f]+\s+(.*)")

# perf annotate format:
#      3.15 :   1d508c3:        testb  $0x8,(%rsi,%r14,1)
_RE_PERF_ANNOTATE = _re.compile(r"^\s*\d+\.\d+\s*:\s+[0-9a-f]+:\s+(.*)")

# GDB disassemble format (with optional => marker and /r hex bytes):
#    0x00005555555fc5c0 <Main()+0>:	push   rbp
# => 0x00005555555fdd64 <main+4>:	pop    rbp
#    0x00005555555fdd6a:	int3
#    0x00005555555fc5c0 <Main()+0>:	55                 	push   rbp   (with /r)
_RE_GDB_DISASM = _re.compile(
    r"^(?:=>)?\s*0x[0-9a-f]+"  # optional => marker, address
    r"(?:\s+<[^>]+>)?:\s+"  # optional <symbol+offset>, then colon
    r"(?:[0-9a-f]{2}(?:\s[0-9a-f]{2})*\s+)?"  # optional hex bytes (/r flag)
    r"(.*)"  # instruction
)

# V8 code comment / ANSI escape lines
_RE_V8_COMMENT = _re.compile(r"^\s*\[3[24]m|\s*\]")

# V8 uses a hybrid syntax: AT&T size suffixes (movl, addl) with Intel operand
# order. Strip the suffix so the Intel parser accepts them.
_RE_SIZE_SUFFIX = _re.compile(
    r"^(REX\.W\s+)?"  # optional REX.W prefix
    r"(j[a-z]+|set[a-z]+|mov[sz]?|lea|add|sub|cmp|test|and|or|xor|sar|shr|shl|"
    r"sal|inc|dec|neg|not|imul|idiv|mul|div|push|pop|call|ret|nop|"
    r"cmov[a-z]+)"
    r"([bwlq])\b",  # size suffix
    _re.IGNORECASE,
)

# Trailing annotations: "<+0x104>", "(comment)", ";; comment"
_RE_TRAILING_ANNOTATION = _re.compile(r"\s+<\+0x[0-9a-f]+>.*$|\s+\(.*\)\s*$|\s+;;.*$")

# REX.W prefix — strip it, the instruction works without it in the assembler
_RE_REX_PREFIX = _re.compile(r"^REX\.W\s+", _re.IGNORECASE)

# Absolute address as jump/call target: "jne 0x7fc5..." or "jne 1d50886" → "jne .L0"
_RE_ABS_JUMP = _re.compile(
    r"^(j[a-z]*|call)\s+(?:0x)?([0-9a-f]{4,})\s*$", _re.IGNORECASE
)


def _clean_asm_for_mca(raw: str) -> str:
    """Strip address/hex prefixes from V8 print-code or perf annotate output."""
    cleaned: list[str] = []
    v8_format = False
    for line in raw.splitlines():
        # V8 print-opt-code: "0xADDR  OFF  HEX  instruction"
        m = _RE_V8_PRINT_CODE.match(line)
        if m:
            v8_format = True
            cleaned.append(m.group(1))
            continue
        # perf annotate: "  pct : addr: instruction"
        m = _RE_PERF_ANNOTATE.match(line)
        if m:
            cleaned.append(m.group(1))
            continue
        # GDB wrapper lines
        if line.startswith("Dump of assembler code") or line.startswith(
            "End of assembler dump"
        ):
            continue
        # GDB disassemble: "   0xADDR <sym+off>:  instruction"
        m = _RE_GDB_DISASM.match(line)
        if m:
            instr = m.group(1).strip()
            if instr:
                cleaned.append(instr)
            continue
        # Skip ANSI escape lines (V8 code comments with [34m prefix)
        if _RE_V8_COMMENT.match(line):
            continue
        # Pass through everything else (plain asm, labels, directives)
        cleaned.append(line)

    if v8_format:
        # V8 print-code uses hybrid syntax: AT&T suffixes + Intel operands.
        # Strip REX.W prefixes, size suffixes, and trailing annotations.
        fixed: list[str] = []
        for line in cleaned:
            line = _RE_TRAILING_ANNOTATION.sub("", line)
            line = _RE_REX_PREFIX.sub("", line)
            line = _RE_SIZE_SUFFIX.sub(r"\1\2", line)
            if line.strip():
                fixed.append(line)
        cleaned = fixed

    # Convert absolute jump/call targets to labels (both formats).
    label_map: dict[str, str] = {}
    fixed = []
    for line in cleaned:
        m = _RE_ABS_JUMP.match(line.strip())
        if m:
            addr = m.group(2)
            if addr not in label_map:
                label_map[addr] = f".L{len(label_map)}"
            line = f"{m.group(1)} {label_map[addr]}"
        fixed.append(line)

    return "\n".join(fixed)


def _filter_mca_output(raw: str) -> str:
    """Filter llvm-mca output to keep only the most useful sections.

    Always keeps: summary, bottleneck analysis, critical sequence,
    instruction info. Only includes resource pressure tables when the
    bottleneck analysis indicates resource pressure is significant (>10%).
    """
    sections: list[tuple[str, list[str]]] = []
    current_name = "summary"
    current_lines: list[str] = []

    # Known section headers
    _SECTION_STARTS = {
        "Cycles with backend pressure": "bottleneck",
        "Critical sequence": "critical",
        "Instruction Info": "instruction_info",
        "Resources:": "resources",
        "Resource pressure per iteration": "pressure_summary",
        "Resource pressure by instruction": "pressure_detail",
        "Timeline view": "timeline",
        "Average Wait times": "wait_times",
    }

    for line in raw.strip().splitlines():
        for prefix, name in _SECTION_STARTS.items():
            if line.startswith(prefix):
                sections.append((current_name, current_lines))
                current_name = name
                current_lines = []
                break
        current_lines.append(line)
    sections.append((current_name, current_lines))

    # Check if resource pressure is a significant bottleneck
    resource_pressure_pct = 0.0
    for name, slines in sections:
        if name == "bottleneck":
            for sl in slines:
                if "Resource Pressure" in sl and "%" in sl:
                    try:
                        resource_pressure_pct = float(
                            sl.split("[")[1].split("%")[0].strip()
                        )
                    except (IndexError, ValueError):
                        pass
                    break

    keep = {
        "summary",
        "bottleneck",
        "critical",
        "instruction_info",
        "timeline",
        "wait_times",
    }
    if resource_pressure_pct > 10:
        keep.update({"resources", "pressure_summary", "pressure_detail"})

    out: list[str] = []
    for name, slines in sections:
        if name in keep:
            # Strip excessive blank lines
            text = "\n".join(slines).strip()
            if text:
                out.append(text)

    return "\n\n".join(out)


# Godbolt (Compiler Explorer) helpers


def llvm_mca(
    assembly: str,
    arch: str = "x64",
    cpu: str | None = None,
    syntax: str = "intel",
    bottleneck: bool = True,
    timeline: bool = False,
) -> str:
    """Run llvm-mca pipeline analysis on raw assembly (e.g. from perf_annotate).

    Simulates how the CPU pipeline would execute the given instructions and
    reports throughput, latency, bottlenecks, and port pressure.
    """
    mca = shutil.which("llvm-mca")
    if mca is None:
        return "Error: llvm-mca not found. Install LLVM (e.g. pacman -S llvm)."

    is_arm64 = arch.lower() in ("arm64", "aarch64")

    src = _clean_asm_for_mca(assembly.strip())

    if is_arm64:
        att = False
    else:
        att = syntax.lower() == "att"
        # Auto-detect AT&T syntax from % register prefixes (e.g. GDB default output)
        if not att and _re.search(
            r"%[re]?[abcd]x|%[re]?[sd]i|%[re]?[bs]p|%r\d+|%xmm", src
        ):
            att = True
        # Prepend syntax directive if not already present
        if ".intel_syntax" not in src and ".att_syntax" not in src:
            if att:
                src = ".att_syntax\n" + src
            else:
                src = ".intel_syntax noprefix\n" + src

    cmd = [
        mca,
        "--noalias",
        "--skip-unsupported-instructions=any",
    ]
    if is_arm64:
        cmd += ["-march=aarch64", "-mtriple=aarch64-linux-gnu"]
    else:
        # output-asm-variant: 0=AT&T, 1=Intel
        cmd.append(f"--output-asm-variant={'0' if att else '1'}")
    if cpu:
        cmd.append(f"--mcpu={cpu}")
    if bottleneck:
        cmd.append("--bottleneck-analysis")
    if timeline:
        cmd.append("--timeline")

    r = subprocess.run(cmd, input=src, capture_output=True, text=True, timeout=30)

    lines: list[str] = []
    header = f"# llvm-mca{f' -mcpu={cpu}' if cpu else ''}"
    lines.append(header)

    if r.stderr.strip():
        for line in r.stderr.strip().splitlines():
            if (
                "found a return instruction" in line
                or "program counter updates" in line
            ):
                continue
            lines.append(line)

    if r.returncode != 0 and not r.stdout.strip():
        lines.append(f"llvm-mca exited with code {r.returncode}")
        return "\n".join(lines)

    if r.stdout.strip():
        lines.append(_filter_mca_output(r.stdout))

    return "\n".join(lines)
