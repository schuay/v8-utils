"""MCP tools for V8 performance investigation: perf, d8, v8log, godbolt, llvm-mca."""

import re as _re
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from .. import config
from .. import jsb as jsb_module
from .. import perf as perf_tools
from .. import v8log
from ._shared import _text_result


_MAX_D8_OUTPUT = 5_000

# Symbol cache: perf_hotspots stores its most recent result per perf_data path
# so that downstream tools can accept "#3" instead of the raw symbol name.
_symbol_cache: dict[str, list[dict]] = {}

# Argument documentation lives on the argument (Annotated[..., Field(...)]), so
# a client sends it as the parameter's own schema description instead of leaving
# the model to match a prose line against a signature by name. These four recur
# across the perf tools; the rest are inline at their parameter.
PERF_DATA_ARG = "path to a perf.data file"
DSO_ARG = 'restrict to a specific shared object, e.g. "libv8.so" or "d8"'
SYMBOL_ARG = "symbol name, unique substring, or #N from perf_hotspots"
CONTEXT_ARG = "lines of context around each hot cluster"


def _resolve_symbol(perf_data: str, symbol: str, **_kw: object) -> str:
    """If *symbol* looks like ``#<n>``, resolve it from the hotspots cache."""
    if symbol.startswith("#"):
        try:
            idx = int(symbol[1:])
        except ValueError:
            raise ValueError(f"Invalid symbol reference: {symbol!r}")
        rows = _symbol_cache.get(perf_data)
        if rows is None:
            raise ValueError(
                f"No cached hotspots for {perf_data!r}. "
                f"Run perf_hotspots first, then use #N references."
            )
        if idx < 1 or idx > len(rows):
            raise ValueError(f"Symbol index {idx} out of range (1–{len(rows)})")
        return rows[idx - 1]["symbol"]
    return symbol


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

_godbolt_compiler_cache: dict[str, list[dict]] | None = None

_GODBOLT_ISET_MAP = {
    "x64": {"amd64", "x86-64", "x86_64"},
    "arm64": {"aarch64", "arm64"},
}

# Default compiler IDs per arch — Godbolt-maintained trunk builds.
_GODBOLT_DEFAULT_COMPILER = {
    "x64": "clang_trunk",
    "arm64": "armv8-clang-trunk",
}

_MCA_DEFAULT_CPU = {"x64": "skylake", "arm64": "cortex-a76"}


def _godbolt_get_compilers(language: str) -> list[dict]:
    """Fetch and cache compiler list from Godbolt. Cached per-language for process lifetime."""
    import httpx

    global _godbolt_compiler_cache
    if _godbolt_compiler_cache is None:
        _godbolt_compiler_cache = {}
    if language not in _godbolt_compiler_cache:
        r = httpx.get(
            f"https://godbolt.org/api/compilers/{language}",
            params={"fields": "id,name,semver,instructionSet"},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        _godbolt_compiler_cache[language] = r.json()
    return _godbolt_compiler_cache[language]


def _godbolt_infer_arch(compiler_id: str, language: str) -> str:
    """Infer arch from a Godbolt compiler's instruction set metadata."""
    for c in _godbolt_get_compilers(language):
        if c.get("id") == compiler_id:
            iset = (c.get("instructionSet") or "").lower()
            for arch, aliases in _GODBOLT_ISET_MAP.items():
                if iset in aliases:
                    return arch
            break
    return "x64"


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def run_d8(
        args: Annotated[
            list[str],
            Field(description='arguments to pass to d8, e.g. ["--prof", "script.js"]'),
        ],
        d8_path: Annotated[
            str | None,
            Field(
                description=(
                    "absolute path to the d8 binary (default: main v8 build). Not"
                    " affected by repo_git_worktree_select -- to run a worktree's"
                    " build, pass its d8 path explicitly."
                )
            ),
        ] = None,
        cwd: Annotated[
            str | None,
            Field(description='working directory for d8 (default: repos["v8"])'),
        ] = None,
        timeout: Annotated[
            int, Field(description="max seconds before killing the process")
        ] = 60,
        output_file: Annotated[
            str | None,
            Field(
                description=(
                    "redirect combined output to this file path instead of capturing"
                )
            ),
        ] = None,
    ) -> CallToolResult:
        """Run the d8 JavaScript shell with the given arguments.

        For benchmarking, use the jsb_run_bench tool instead.

        stdout and stderr are combined into a single stream.

        Example -- run a JetStream3 line item:
          args: ["cli.js", "--", "regexp-octane"]
          cwd:  "/absolute/path/to/JetStream3"
        """
        cfg = config.load()
        if d8_path:
            d8 = Path(d8_path).expanduser()
        else:
            d8 = cfg.v8_out / cfg.default_build / "d8"
        if not d8.exists():
            raise ValueError(f"d8 not found: {d8}")

        cmd = [str(d8), *args]
        stdout = open(output_file, "w") if output_file else subprocess.PIPE
        try:
            result = subprocess.run(
                cmd,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                errors="replace",
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            return _text_result(f"Error: d8 timed out after {timeout}s")
        except Exception as e:
            return _text_result(f"Error: {e}")
        finally:
            if output_file:
                stdout.close()

        parts: list[str] = []
        if output_file:
            parts.append(f"[output → {output_file}]")
        elif result.stdout:
            parts.append(result.stdout)
        if result.returncode not in (0, 1):
            parts.append(f"[exit {result.returncode}]")

        out = "\n".join(parts).strip()
        if not out:
            out = "(no output)"
        if len(out) > _MAX_D8_OUTPUT:
            out = (
                out[:_MAX_D8_OUTPUT]
                + f"\n\n[truncated — {len(out) - _MAX_D8_OUTPUT:,} more chars. "
                "Use output_file to redirect large output to a file.]"
            )
        return _text_result(out)

    @mcp.tool()
    def jsb_run_bench(
        lineitems: Annotated[
            list[str] | None,
            Field(
                description=(
                    'benchmark story names, e.g. ["regexp-octane", "chai-wtb"].'
                    " Omit to run the full suite."
                )
            ),
        ] = None,
        binaries: Annotated[
            list[str],
            Field(
                description=(
                    "absolute paths to JS shell binaries (d8, jsc, etc.), each"
                    ' optionally followed by ":flags". Pass the executable file'
                    " itself, NOT a build directory. Examples:"
                    ' ["/home/user/src/v8/v8/out/x64.release/d8",'
                    ' "/home/user/src/v8/feature-wt/out/x64.release/d8:--turbolev-future",'
                    ' "/home/user/WebKit/WebKitBuild/Release/bin/jsc"]'
                )
            ),
        ] = [],
        runs: Annotated[int, Field(description="number of runs per variant")] = 5,
        suite: Annotated[str, Field(description='"js2" or "js3"')] = "js3",
        record: Annotated[
            str | None,
            Field(
                description=(
                    "profiling mode -- omit to run for scores (the default)."
                    ' Options: "perf" records a linux-perf trace and returns the'
                    " perf.data path for perf_hotspots/perf_annotate;"
                    ' "perf_upload" does the same and uploads it via pprof;'
                    ' "v8log" records a v8.log and returns its path for'
                    " v8log_analyze. Every record mode requires exactly one"
                    " binary."
                )
            ),
        ] = None,
    ) -> CallToolResult:
        """Run a JetStream2/3 story with one or more JS shell binaries and return scores.

        Returns a comparison table with mean, stdev, delta, p-value
        (Welch's t-test), and confidence (high/medium/low) per metric.
        """
        cfg = config.load()
        js3 = suite.lower() != "js2"
        key = "js3" if js3 else "js2"
        suite_dir = cfg.repos[key].path
        suite_label = "JS3" if js3 else "JS2"
        # Name the path, and name it HERE. The suite is reached as
        # `suite_dir/cli.js`, so a missing one otherwise surfaces as d8 failing to
        # open a file several layers down -- and a caller that cannot see the
        # configured path has no way to tell "not installed" from "installed
        # somewhere else". Sandboxed agents read that as "JetStream is missing"
        # and silently skip the measurement, which on a perf job is the whole
        # deliverable. The path is not guessable either (the real one is
        # `.../v8-perf/benchmarks/JetStream/v3.0-custom`, containing no "js3"),
        # so stating it is the difference between an actionable error and a dead
        # end.
        if not (suite_dir / "cli.js").is_file():
            raise FileNotFoundError(
                f"{suite_label} not found: no cli.js under {suite_dir} "
                f"(configured as repos.{key} in {config.CONFIG_PATH}). "
                "Point that at a JetStream checkout, or pass a suite that is "
                "installed."
            )

        for b in binaries:
            path_part = b.split(":")[0].strip()
            if not Path(path_part).is_absolute():
                raise ValueError(
                    f"binary must be an absolute path, got {path_part!r}. "
                    f"Example: /home/user/src/v8/v8/out/x64.release/d8"
                )
        variants = [jsb_module.Variant.parse(b) for b in binaries]
        for v in variants:
            d8 = v.d8(cfg.v8_out)
            if d8.is_dir():
                raise ValueError(
                    f"{d8} is a directory, not a binary. "
                    f'Pass the executable itself, e.g. "{d8}/d8".'
                )
            if not d8.exists():
                raise ValueError(f"binary not found: {d8}")

        if record is not None:
            _RECORD_MODES = ("perf", "perf_upload", "v8log")
            if record not in _RECORD_MODES:
                raise ValueError(
                    f"record must be one of {_RECORD_MODES}, got {record!r}"
                )
            if len(variants) != 1:
                raise ValueError("record mode requires exactly one binary")
            v = variants[0]
            if record == "v8log":
                return _text_result(
                    str(jsb_module.run_v8log(v, suite_dir, lineitems, cfg.v8_out))
                )
            return _text_result(
                jsb_module.run_perf(
                    v,
                    suite_dir,
                    lineitems,
                    cfg.v8_out,
                    cfg.perf_script,
                    upload=(record == "perf_upload"),
                )
            )

        results = jsb_module.run_round_robin(
            variants, suite_dir, lineitems, runs, js3, cfg.v8_out
        )

        return _text_result(
            jsb_module.format_table(lineitems, suite_label, runs, variants, results)
        )

    @mcp.tool()
    def perf_stat(
        stat_file: Annotated[
            str,
            Field(
                description=(
                    "path to a file containing `perf stat` text output, saved via"
                    " `perf stat -o <file>` or stderr redirection"
                )
            ),
        ],
    ) -> CallToolResult:
        """Parse a saved `perf stat` output file into structured counter data.

        Returns elapsed_seconds and a list of counters with their values and
        human-readable notes (e.g. "3.45 CPUs utilized").
        """
        data = perf_tools.parse_stat(stat_file)
        lines = []
        if data.get("elapsed_seconds") is not None:
            lines.append(f"elapsed: {data['elapsed_seconds']:.3f}s")
            lines.append("")
        for c in data.get("counters", []):
            val = f"{c['value']:>15,.0f}  {c['counter']}"
            if c.get("note"):
                val += f"  # {c['note']}"
            lines.append(val)
        return _text_result("\n".join(lines) if lines else "No counters found.")

    @mcp.tool()
    def perf_hotspots(
        perf_data: Annotated[str, Field(description=PERF_DATA_ARG)],
        dso: Annotated[str | None, Field(description=DSO_ARG)] = None,
        n: Annotated[int, Field(description="number of symbols to return")] = 30,
    ) -> CallToolResult:
        """Return the top N hot symbols from a perf.data file.

        Each entry includes self_pct (exclusive time) and total_pct (inclusive
        time including callees), plus the symbol name and shared object.
        Sorted by self_pct descending.

        Typical workflow: perf_hotspots -> perf_flamegraph -> perf_annotate.
        """
        rows = perf_tools.hotspots(perf_data, dso=dso, n=n)
        if not rows:
            return _text_result("No symbols found.")
        _symbol_cache[perf_data] = rows
        idx_w = len(str(len(rows)))
        lines = [f"{'#':>{idx_w}}  {'self%':>6}  {'total%':>6}  {'dso':<20}  symbol"]
        lines.append("-" * len(lines[0]))
        for i, r in enumerate(rows, 1):
            total = f"{r['total_pct']:.1f}" if r.get("total_pct") is not None else "—"
            lines.append(
                f"{i:>{idx_w}}  {r['self_pct']:5.1f}%  {total:>5}%  {r['dso']:<20}  {r['symbol']}"
            )
        return _text_result("\n".join(lines))

    @mcp.tool()
    def perf_callers(
        perf_data: Annotated[str, Field(description=PERF_DATA_ARG)],
        symbol: Annotated[str, Field(description=SYMBOL_ARG)],
        n: Annotated[
            int, Field(description="max lines of call-graph detail to return")
        ] = 20,
    ) -> CallToolResult:
        """Show who calls a hot symbol and with what sample weight.

        Returns the call-graph section for the symbol from perf report in
        caller mode, so the tree reads upward (direct callers nearest, then
        their callers above).  Use this to understand whether hotness is
        self-time or propagated from a call site.
        """
        symbol = _resolve_symbol(perf_data, symbol)
        return _text_result(perf_tools.callers(perf_data, symbol, n=n))

    @mcp.tool()
    def perf_annotate(
        perf_data: Annotated[str, Field(description=PERF_DATA_ARG)],
        symbol: Annotated[
            str, Field(description="exact symbol name or #N from perf_hotspots")
        ],
        dso: Annotated[str | None, Field(description=DSO_ARG)] = None,
        min_pct: Annotated[
            float, Field(description="minimum sample % to qualify as hot")
        ] = 0.5,
        context: Annotated[int, Field(description=CONTEXT_ARG)] = 8,
    ) -> CallToolResult:
        """Annotated disassembly for a symbol, with smart hot-region extraction.

        Shows the 20 hottest instructions and contiguous hot code blocks
        (>= min_pct), each expanded by +/-context lines and sorted by peak heat.

        Line numbers are included so you can call perf_annotate_read_around
        to explore surrounding code.
        """
        symbol = _resolve_symbol(perf_data, symbol, dso=dso)
        data = perf_tools.annotate(
            perf_data, symbol, dso=dso, min_pct=min_pct, context=context
        )
        lines = [
            f"{data['symbol']}  ({data['total_lines']} lines, min_pct={data['min_pct_threshold']}%)"
        ]
        if data.get("parse_warnings"):
            for w in data["parse_warnings"]:
                lines.append(f"warning: {w}")
        # Top instructions
        lines.append("")
        lines.append("Top instructions:")
        lines.append(f"{'line':>6}  {'pct':>6}  {'addr':<14}  asm")
        lines.append("-" * 60)
        for instr in data.get("top_instructions", []):
            lines.append(
                f"{instr['lineno']:6}  {instr['pct']:5.1f}%  {instr['addr']:<14}  {instr['asm']}"
            )
        # Hot blocks
        for i, block in enumerate(data.get("hot_blocks", [])):
            lines.append("")
            lines.append(
                f"Hot block #{i + 1} (lines {block['line_range']}, peak {block['peak_pct']:.1f}%):"
            )
            lines.append(block["content"])
        return _text_result("\n".join(lines))

    @mcp.tool()
    def perf_annotate_read_around(
        perf_data: Annotated[str, Field(description=PERF_DATA_ARG)],
        symbol: Annotated[
            str, Field(description="symbol name or #N from perf_hotspots")
        ],
        line: Annotated[
            int, Field(description="1-based line number to centre the window on")
        ],
        context: Annotated[
            int, Field(description="lines before and after to include")
        ] = 30,
        dso: Annotated[
            str | None,
            Field(
                description=(
                    "shared object filter; must match the perf_annotate call if"
                    " one was used"
                )
            ),
        ] = None,
    ) -> CallToolResult:
        """Read a window of annotated disassembly around a specific line number.

        Use this after perf_annotate to explore regions of interest.  Line
        numbers are as reported in perf_annotate's top_instructions and
        hot_blocks fields.  Each output line is prefixed with its line number
        for further navigation.
        """
        symbol = _resolve_symbol(perf_data, symbol, dso=dso)
        return _text_result(
            perf_tools.annotate_read_around(
                perf_data, symbol, line, context=context, dso=dso
            )
        )

    @mcp.tool()
    def perf_flamegraph(
        perf_data: Annotated[str, Field(description=PERF_DATA_ARG)],
        focus_symbol: Annotated[
            str | None,
            Field(
                description=(
                    "restrict to call trees whose root matches this substring, or"
                    ' #N from perf_hotspots, e.g. "RegExpPrototypeExec" or "#3"'
                )
            ),
        ] = None,
        dso: Annotated[str | None, Field(description=DSO_ARG)] = None,
        min_pct: Annotated[
            float, Field(description="omit paths below this % of total samples")
        ] = 0.5,
        depth: Annotated[
            int, Field(description="maximum call-chain depth to expand")
        ] = 8,
    ) -> CallToolResult:
        """Aggregated text flamegraph: all hot call paths in one view.

        Shows root-to-leaf call chains sorted by absolute sample percentage, so
        the dominant execution paths are immediately visible without iterative
        perf_callers traversal.

        Typical workflow:
          1. perf_hotspots  -- find the hottest symbols
          2. perf_flamegraph(focus_symbol=X)  -- understand full call context
          3. perf_annotate  -- drill into hot instructions

        When focus_symbol is set, shows the *inclusive* (total) cost breakdown
        for that symbol -- where its children spend time.  Percentages are
        absolute (% of total samples).  This is the primary use case.

        Without focus_symbol, shows self-time callee paths for all symbols.
        """
        if focus_symbol is not None:
            focus_symbol = _resolve_symbol(perf_data, focus_symbol, dso=dso)
        return _text_result(
            perf_tools.flamegraph(
                perf_data,
                focus_symbol=focus_symbol,
                dso=dso,
                min_pct=min_pct,
                depth=depth,
            )
        )

    @mcp.tool()
    def perf_tma(
        perf_data: Annotated[str, Field(description=PERF_DATA_ARG)],
        symbol: Annotated[
            str | None,
            Field(
                description=(
                    "filter to symbols containing this substring, or #N from"
                    " perf_hotspots"
                )
            ),
        ] = None,
        n: Annotated[
            int, Field(description="max symbols to return, sorted by cycles_pct")
        ] = 20,
    ) -> CallToolResult:
        """Microarchitecture bottleneck analysis (TMA Level 1) per symbol.

        Always safe to call -- returns a message when the perf.data was not
        recorded with TMA events.

        Intensity fields = event_pct / cycles_pct for each symbol:
          ~1.0  proportional to cycle share (average)
          >1.0  disproportionately high -- likely bottleneck
          <1.0  below average

        To enable: re-record with linux-perf-d8.py --topdown
        (Intel Skylake-SP; requires topdown-* kernel PMU events)

        Recommended workflow:
          1. perf_hotspots       -- rank hot symbols
          2. perf_tma            -- characterise bottleneck (works or tells you how)
          3. perf_flamegraph     -- understand call context
          4. perf_annotate       -- inspect hot instructions
        """
        if symbol is not None:
            symbol = _resolve_symbol(perf_data, symbol)
        data = perf_tools.tma(perf_data, symbol=symbol, n=n)
        if not data.get("available"):
            return _text_result(data.get("message", "TMA data not available."))

        has_mem = data.get("has_mem_detail", False)
        hdr = f"{'cyc%':>6}  {'FE':>5}  {'Ret':>5}  {'Bad':>5}"
        if has_mem:
            hdr += f"  {'Mem':>5}"
        hdr += f"  {'dominant':<24}  symbol"
        lines = [hdr, "-" * len(hdr)]
        for s in data.get("symbols", []):
            row = (
                f"{s['cycles_pct']:5.1f}%"
                f"  {s['fe_intensity']:5.2f}"
                f"  {s['retiring_intensity']:5.2f}"
                f"  {s['bad_spec_intensity']:5.2f}"
            )
            if has_mem:
                mem = s.get("mem_intensity")
                row += f"  {mem:5.2f}" if mem is not None else "      —"
            row += f"  {s['dominant']:<24}  {s['symbol']}"
            lines.append(row)
        return _text_result("\n".join(lines))

    @mcp.tool()
    def perf_diff(
        baseline: Annotated[str, Field(description="path to the baseline perf.data")],
        experiment: Annotated[
            str, Field(description="path to the experiment perf.data")
        ],
        dso: Annotated[str | None, Field(description=DSO_ARG)] = None,
        n: Annotated[int, Field(description="number of symbols to return")] = 30,
    ) -> CallToolResult:
        """Compare two perf profiles: what got hotter or cooler?

        Returns the top N symbols sorted by |delta_pct|, so the biggest
        changes appear first regardless of direction.
        """
        rows = perf_tools.diff(baseline, experiment, dso=dso, n=n)
        if not rows:
            return _text_result("No symbol differences found.")
        lines = [f"{'delta':>8}  {'base%':>6}  {'after%':>7}  {'dso':<20}  symbol"]
        lines.append("-" * len(lines[0]))
        for r in rows:
            base = (
                f"{r['baseline_pct']:.1f}%"
                if r.get("baseline_pct") is not None
                else "new"
            )
            after = (
                f"{r['after_pct']:.1f}%" if r.get("after_pct") is not None else "gone"
            )
            delta = r.get("delta_pct")
            delta_s = f"{delta:+.1f}%" if delta is not None else "—"
            lines.append(
                f"{delta_s:>8}  {base:>6}  {after:>7}  {r['dso']:<20}  {r['symbol']}"
            )
        return _text_result("\n".join(lines))

    @mcp.tool()
    def d8_trace_index(
        path: Annotated[str, Field(description="path to the trace file")],
    ) -> CallToolResult:
        """Build a table of contents for a V8 trace file.

        Recognizes sections from --trace-turbo-graph, --print-maglev-graphs,
        --trace-maglev-graph-building, --trace-opt, --trace-deopt, and
        --print-code. Use the line numbers to navigate with read_around.
        """
        try:
            return _text_result(_build_trace_index(path))
        except FileNotFoundError:
            return _text_result(f"File not found: {path}")

    @mcp.tool()
    def llvm_mca(
        assembly: Annotated[
            str,
            Field(description="assembly text (from V8 JIT / perf / GDB disassemble)"),
        ],
        arch: Annotated[
            str, Field(description='target architecture: "x64" or "arm64"')
        ] = "x64",
        cpu: Annotated[
            str | None,
            Field(
                description=(
                    "CPU model for scheduling simulation. x64: skylake, znver3,"
                    " alderlake, znver4, ... arm64: neoverse-n1, neoverse-v2,"
                    " cortex-a76, cortex-x2, ..."
                )
            ),
        ] = None,
        syntax: Annotated[
            str,
            Field(
                description=(
                    'x64 only: "intel" (default) or "att"; auto-detected from'
                    " GDB/perf output. Ignored for arm64."
                )
            ),
        ] = "intel",
        bottleneck: Annotated[
            bool,
            Field(
                description=(
                    "include bottleneck analysis showing what limits throughput"
                )
            ),
        ] = True,
        timeline: Annotated[
            bool,
            Field(description="include cycle-by-cycle pipeline timeline (verbose)"),
        ] = False,
    ) -> CallToolResult:
        """Run llvm-mca pipeline analysis on raw assembly (e.g. from perf_annotate).

        Simulates how the CPU pipeline would execute the given instructions and
        reports throughput, latency, bottlenecks, and port pressure.
        """
        mca = shutil.which("llvm-mca")
        if mca is None:
            return _text_result(
                "Error: llvm-mca not found. Install LLVM (e.g. pacman -S llvm)."
            )

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
            return _text_result("\n".join(lines))

        if r.stdout.strip():
            lines.append(_filter_mca_output(r.stdout))

        return _text_result("\n".join(lines))

    @mcp.tool()
    def godbolt_compile(
        source: Annotated[str, Field(description="the source code to compile")],
        arch: Annotated[str, Field(description='"x64" or "arm64"')] = "x64",
        compiler: Annotated[
            str | None,
            Field(
                description=(
                    "exact Godbolt compiler ID (default: clang_trunk for x64,"
                    " armv8-clang-trunk for arm64). Use godbolt_list_compilers"
                    " to find other IDs."
                )
            ),
        ] = None,
        language: Annotated[str, Field(description='"c++" or "c"')] = "c++",
        flags: Annotated[
            str, Field(description="compiler flags (default: V8 release flags)")
        ] = "-O3 -fno-strict-aliasing -fno-omit-frame-pointer",
        mca: Annotated[
            bool,
            Field(
                description=(
                    "run llvm-mca pipeline analysis (clang only). Shows"
                    " throughput, bottlenecks, and port pressure per instruction."
                )
            ),
        ] = True,
        opt_remarks: Annotated[
            bool,
            Field(
                description=(
                    "include LLVM optimization pass remarks (clang only). Shows"
                    " which optimizations fired or failed and why."
                )
            ),
        ] = False,
    ) -> CallToolResult:
        """Compile a code snippet on Godbolt and return the assembly output.

        By default uses the latest clang trunk and runs llvm-mca analysis.
        """
        import httpx

        compiler_id = compiler or _GODBOLT_DEFAULT_COMPILER.get(arch)
        if compiler_id is None:
            return _text_result(
                f"Unknown arch {arch!r}. Use 'x64' or 'arm64', "
                f"or pass an explicit compiler ID."
            )

        # When compiler is explicitly specified, infer arch from metadata for MCA.
        if compiler is not None:
            arch = _godbolt_infer_arch(compiler_id, language)

        if (mca or opt_remarks) and "clang" not in compiler_id.lower():
            return _text_result("Error: mca and opt_remarks require a Clang compiler.")

        options: dict = {
            "userArguments": flags,
            "filters": {
                "intel": True,
                "demangle": True,
                "commentOnly": True,
                "directives": True,
            },
        }

        if mca:
            cpu = _MCA_DEFAULT_CPU.get(arch, "")
            mca_arg = f"-mcpu={cpu}" if cpu else ""
            options["tools"] = [{"id": "llvm-mcatrunk", "args": mca_arg}]

        if opt_remarks:
            options["compilerOptions"] = {"produceOptInfo": True}

        r = httpx.post(
            f"https://godbolt.org/api/compiler/{compiler_id}/compile",
            json={"source": source, "lang": language, "options": options},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

        lines: list[str] = [f"# {compiler_id} {flags}"]

        stderr_lines = data.get("stderr") or []
        if stderr_lines:
            for s in stderr_lines:
                lines.append(s.get("text", ""))
            lines.append("")

        asm_lines = data.get("asm") or []
        for a in asm_lines:
            lines.append(a.get("text", ""))

        if mca:
            for tool_entry in data.get("tools") or []:
                if tool_entry.get("id") == "llvm-mcatrunk":
                    lines.append("")
                    lines.append("# --- llvm-mca analysis ---")
                    for s in tool_entry.get("stderr") or []:
                        lines.append(s.get("text", ""))
                    for s in tool_entry.get("stdout") or []:
                        lines.append(s.get("text", ""))

        if opt_remarks:
            opt_output = data.get("optOutput") or []
            if opt_output:
                lines.append("")
                lines.append("# --- optimization remarks ---")
                for opt_type in ("Missed", "Passed", "Analysis"):
                    entries = [o for o in opt_output if o.get("optType") == opt_type]
                    if not entries:
                        continue
                    lines.append(f"# {opt_type} ({len(entries)}):")
                    for o in entries:
                        loc = o.get("DebugLoc") or {}
                        loc_str = (
                            f"{loc.get('File', '?')}:{loc.get('Line', '?')}"
                            if loc
                            else ""
                        )
                        fn = o.get("Function", "")
                        display = o.get("displayString", "")
                        lines.append(f"  [{fn}] {loc_str}: {display}")

        return _text_result("\n".join(lines))

    @mcp.tool()
    def godbolt_list_compilers(
        language: Annotated[str, Field(description='"c++", "c", "rust", etc.')] = "c++",
        filter: Annotated[
            str | None,
            Field(
                description=(
                    'substring match on name/instructionSet, e.g. "clang 19" or "arm64"'
                )
            ),
        ] = None,
    ) -> CallToolResult:
        """List available compilers on Godbolt for a language. Use filter to narrow results."""
        compilers = _godbolt_get_compilers(language)

        if filter:
            needle = filter.lower()
            compilers = [
                c
                for c in compilers
                if needle in (c.get("id") or "").lower()
                or needle in (c.get("name") or "").lower()
                or needle in (c.get("instructionSet") or "").lower()
            ]

        lines = [f"{'id':<30} {'name':<45} {'instructionSet'}"]
        lines.append("-" * len(lines[0]))
        for c in compilers:
            lines.append(
                f"{c.get('id', ''):<30} {c.get('name', ''):<45} {c.get('instructionSet', '')}"
            )

        if len(lines) == 2:
            return _text_result("No compilers matched the filter.")

        return _text_result("\n".join(lines))

    @mcp.tool()
    def v8log_analyze(
        log_path: Annotated[str, Field(description="path to a v8.log file")],
        command: Annotated[
            str,
            Field(description="one of deopts, ics, maps, fn, profile, vms"),
        ] = "deopts",
        top: Annotated[int, Field(description="max rows to show")] = 20,
        filter: Annotated[
            str | None,
            Field(description='function name glob to filter results, e.g. "parse*"'),
        ] = None,
        pattern: Annotated[
            str | None,
            Field(
                description=("function name glob for the fn command (required for fn)")
            ),
        ] = None,
        verbose: Annotated[
            bool,
            Field(description="show full map-details strings (maps command only)"),
        ] = False,
    ) -> CallToolResult:
        """Analyze a V8 log file (v8.log) produced by d8 --prof --log-ic --log-maps.

        Commands:
          deopts   -- deoptimization summary (uses: top, filter)
          ics      -- inline cache summary (uses: top, filter)
          maps     -- map transition summary (uses: top, verbose)
          fn       -- function drill-down (requires: pattern)
          profile  -- tick profile flat view (uses: top, filter)
          vms      -- VM state breakdown
        """
        path = Path(log_path).expanduser()
        if not path.exists():
            raise ValueError(f"File not found: {path}")

        log = v8log.V8Log.parse(path)

        if command == "deopts":
            summary = v8log.analyze_deopts(log, top=top, filter_pat=filter)
            return _text_result(v8log.format_deopts(summary))
        if command == "ics":
            summary = v8log.analyze_ics(log, top=top, filter_pat=filter)
            return _text_result(v8log.format_ics(summary))
        if command == "maps":
            summary = v8log.analyze_maps(log, top=top)
            return _text_result(v8log.format_maps(summary, verbose=verbose))
        if command == "fn":
            if not pattern:
                raise ValueError("The fn command requires a pattern argument.")
            summary = v8log.analyze_fn(log, pattern=pattern)
            return _text_result(v8log.format_fn(summary))
        if command == "profile":
            summary = v8log.analyze_profile(log, top=top, filter_pat=filter)
            return _text_result(v8log.format_profile(summary))
        if command == "vms":
            summary = v8log.analyze_vms(log)
            return _text_result(v8log.format_vms(summary))

        raise ValueError(
            f"Unknown command {command!r}. "
            "Use 'deopts', 'ics', 'maps', 'fn', 'profile', or 'vms'."
        )
