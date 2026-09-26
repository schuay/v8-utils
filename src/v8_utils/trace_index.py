"""Tables of contents for V8 trace output: compilation boundaries, phases,
optimization and deoptimization events, and code objects, with line numbers."""

import re as _re
from pathlib import Path


# d8 trace index patterns

# Each pattern: (compiled regex, category, group index for label extraction)
_TRACE_PATTERNS: list[tuple[_re.Pattern[str], str, int | None]] = [
    # Turbofan compilation boundaries
    (
        _re.compile(r"^Begin compiling method (.+) using TurboFan"),
        "turbofan",
        1,
    ),
    (
        _re.compile(r"^Finished compiling method (.+) using TurboFan"),
        "turbofan",
        1,
    ),
    # Maglev compilation boundary
    (
        _re.compile(r"^Compiling 0x[0-9a-f]+ <JSFunction (\S+) .+> with Maglev"),
        "maglev",
        1,
    ),
    # Maglev inlining (must be before generic phase pattern)
    (
        _re.compile(
            r"^----- Inlining 0x[0-9a-f]+ <SharedFunctionInfo (\S+)> with bytecode"
        ),
        "maglev-inline",
        1,
    ),
    # Turbofan / Turboshaft / Maglev phases: "----- <phase> -----"
    # Matches graph phases, schedule, instruction sequence, bytecode array, etc.
    (_re.compile(r"^----- (.+?) -----\s*$"), "phase", 1),
    # trace-opt: marking for optimization
    (
        _re.compile(
            r"^\[marking 0x[0-9a-f]+ <JSFunction (\S+) .+> for optimization to (\S+),"
        ),
        "opt",
        None,  # custom extraction below
    ),
    # trace-opt: compiling method
    (
        _re.compile(
            r"^\[compiling method 0x[0-9a-f]+ <JSFunction (\S+) .+> \(target (\S+)\)"
        ),
        "compile",
        None,
    ),
    # trace-opt: completed compiling
    (
        _re.compile(
            r"^\[completed compiling 0x[0-9a-f]+ <JSFunction (\S+) .+> \(target (\S+)\)"
        ),
        "compiled",
        None,
    ),
    # trace-deopt: bailout
    (
        _re.compile(
            r"^\[bailout \(kind: ([^,]+), reason: ([^)]+)\): begin\. deoptimizing 0x[0-9a-f]+ <JSFunction (\S+)"
        ),
        "deopt",
        None,
    ),
    # print-code: Code object header
    (
        _re.compile(r"^kind = (\S+)"),
        "code",
        1,
    ),
]


def _extract_label(pattern_idx: int, m: _re.Match[str]) -> str:
    """Extract a human-readable label from a regex match."""
    cat = _TRACE_PATTERNS[pattern_idx][1]
    if cat == "opt":
        return f"marking {m.group(1)} → {m.group(2)}"
    if cat in ("compile", "compiled"):
        return f"{m.group(1)} (target {m.group(2)})"
    if cat == "deopt":
        return f"{m.group(3)}: {m.group(2)} ({m.group(1)})"
    group_idx = _TRACE_PATTERNS[pattern_idx][2]
    if group_idx is not None:
        return m.group(group_idx)
    return m.group(0)


def _build_trace_index(path: str) -> str:
    """Scan a trace file and return a table of contents."""
    text = Path(path).read_text(errors="replace")
    lines = text.split("\n")

    entries: list[tuple[int, str, str]] = []  # (line_no, category, label)

    # Track current compilation context for indentation
    for i, line in enumerate(lines):
        for pat_idx, (pattern, cat, _) in enumerate(_TRACE_PATTERNS):
            m = pattern.match(line)
            if m:
                label = _extract_label(pat_idx, m)
                entries.append((i + 1, cat, label))
                break

    if not entries:
        return f"No trace sections found in {path} ({len(lines)} lines)"

    # Format output with indentation for phases within compilations
    out: list[str] = [f"{path} ({len(lines)} lines, {len(entries)} sections)"]
    out.append("")

    in_compilation = False
    for line_no, cat, label in entries:
        prefix = f"L{line_no:<8}"
        if cat in ("turbofan", "maglev"):
            if "Finished" in label or "completed" in label:
                in_compilation = False
                out.append(f"{prefix}[{cat}] Finished {label}")
            else:
                in_compilation = True
                out.append(f"{prefix}[{cat}] {label}")
        elif cat == "phase":
            indent = "  " if in_compilation else ""
            out.append(f"{prefix}{indent}[phase] {label}")
        elif cat == "maglev-inline":
            out.append(f"{prefix}  [inline] {label}")
        elif cat == "opt":
            out.append(f"{prefix}[opt] {label}")
        elif cat == "compile":
            out.append(f"{prefix}[compile] {label}")
        elif cat == "compiled":
            out.append(f"{prefix}[compiled] {label}")
        elif cat == "deopt":
            out.append(f"{prefix}[deopt] {label}")
        elif cat == "code":
            out.append(f"{prefix}[code] {label}")
        else:
            out.append(f"{prefix}[{cat}] {label}")

    return "\n".join(out)


# llvm-mca helpers

# V8 print-opt-code format:
#   0x7fc5e000a500    80  453bd8               cmpl r11,r8


def d8_trace_index(
    path: str,
) -> str:
    """Build a table of contents for a V8 trace file.

    Recognizes sections from --trace-turbo-graph, --print-maglev-graphs,
    --trace-maglev-graph-building, --trace-opt, --trace-deopt, and
    --print-code. Use the line numbers to navigate with read_around.
    """
    try:
        return _build_trace_index(path)
    except FileNotFoundError:
        return f"File not found: {path}"
