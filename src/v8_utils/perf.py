"""Linux perf analysis tools.

All functions invoke the `perf` binary via subprocess and parse its
--stdio output.  No perf Python bindings required.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


# ── subprocess helper ─────────────────────────────────────────────────────────


def _run(args: list[str]) -> str:
    r = subprocess.run(args, capture_output=True, text=True)
    # perf exits non-zero for harmless warnings; only fail when stdout is empty
    if r.returncode != 0 and not r.stdout.strip():
        raise RuntimeError(
            f"`{' '.join(args[:4])}` failed (exit {r.returncode}): "
            f"{r.stderr.strip()[:500]}"
        )
    return r.stdout


# ── perf stat ─────────────────────────────────────────────────────────────────


def parse_stat(stat_file: str) -> dict:
    """Parse a saved `perf stat` output file into structured data.

    The file should be the text captured from `perf stat -o <file>` or
    redirected from stderr.  Returns a dict with:
      elapsed_seconds: wall-clock time, or None if not found
      counters: list of {counter, value, note} dicts, sorted by counter name
    """
    text = Path(stat_file).read_text(errors="replace")
    counters: list[dict] = []
    elapsed: float | None = None

    for line in text.splitlines():
        # Counter line:  "   12,345.67 msec task-clock  #  3.45 CPUs utilized"
        #            or: "        1,234      context-switches  #  100.00 K/sec"
        m = re.match(
            r"^\s+([\d,]+(?:\.\d+)?)\s+(?:msec\s+)?(\S.*?\S)\s{2,}(?:#\s+(.*))?$",
            line,
        )
        if m:
            raw = m.group(1).replace(",", "")
            try:
                value = float(raw)
            except ValueError:
                continue
            counters.append(
                {
                    "counter": m.group(2).strip(),
                    "value": value,
                    "note": m.group(3).strip() if m.group(3) else None,
                }
            )
            continue

        m2 = re.match(r"^\s+([\d.]+)\s+seconds time elapsed", line)
        if m2:
            elapsed = float(m2.group(1))

    counters.sort(key=lambda c: c["counter"])
    return {"elapsed_seconds": elapsed, "counters": counters}


# ── perf report (flat profile) ────────────────────────────────────────────────

# Anchored on the stable [X] type marker; handles any number of leading % columns
# (e.g. perf configs that show children%, self%, and possibly more) and any number
# of intermediate columns (command, CPU, period, …).  The greedy (?:\s+\S+)*
# consumes all tokens before [X], then backtracks to leave the final one as DSO.
#
#   no-children:   "    12.34%  d8  libv8.so  [.] v8::Foo::Bar<int>"
#   with-children: "    20.00%    12.34%  d8  libv8.so  [.] v8::Foo::Bar<int>"
#
# Group 1: first (leading) %  — self% in no-children mode, children% otherwise
# Group 2: DSO  (last token before [X])
# Group 3: symbol name
_REPORT_RE = re.compile(
    r"^\s*([\d.]+)%"  # leading overhead %
    r"(?:\s+[\d.]+%)*"  # zero or more additional % columns
    r"(?:\s+\S+)*"  # command + any other intermediate columns (greedy, backtracks)
    r"\s+(\S+)"  # DSO — last token before the type marker
    r"\s+\[.\]\s+(.+)$"  # [type] symbol
)


def _parse_flat_report(text: str) -> dict[str, tuple[float, str]]:
    """Return {symbol: (overhead_pct, dso)} from perf report --stdio output."""
    result: dict[str, tuple[float, str]] = {}
    for line in text.splitlines():
        m = _REPORT_RE.match(line)
        if m:
            pct, dso, sym = float(m.group(1)), m.group(2), m.group(3).strip()
            if sym not in result:  # keep first (highest) occurrence
                result[sym] = (pct, dso)
    return result


def hotspots(
    perf_data: str,
    dso: str | None = None,
    n: int = 30,
) -> list[dict]:
    """Return the top N hot symbols by self%, with total% alongside.

    self_pct:  time spent directly in this symbol (exclusive)
    total_pct: time spent in this symbol or its callees (inclusive)

    dso: restrict to a specific shared object, e.g. "libv8.so" or "d8"
    """
    base = ["perf", "report", "--stdio", "--no-header", "-i", perf_data]
    if dso:
        base += ["--dso", dso]
    self_data = _parse_flat_report(_run(base + ["--no-children"]))
    total_data = _parse_flat_report(_run(base))

    top = sorted(self_data.items(), key=lambda x: x[1][0], reverse=True)[:n]
    return [
        {
            "symbol": sym,
            "dso": sym_dso,
            "self_pct": self_pct,
            "total_pct": total_data.get(sym, (None,))[0],
        }
        for sym, (self_pct, sym_dso) in top
    ]


# ── perf callers ──────────────────────────────────────────────────────────────


def callers(perf_data: str, symbol: str, n: int = 20) -> str:
    """Return the call-graph section above a symbol: who calls it and at what %.

    Uses caller-mode call graphs so the tree reads upward (callers on top).
    Returns the raw perf-report text block for the matching symbol, which the
    LLM can interpret as a call tree.  Up to n lines of call-graph detail.
    """
    args = [
        "perf",
        "report",
        "--stdio",
        "--no-header",
        "-g",
        "caller,0.01,callee",
        "--no-children",
        "-i",
        perf_data,
    ]
    # --symbol-filter is available in perf >= 4.x and limits noise significantly
    args += ["--symbol-filter", symbol]
    text = _run(args)

    lines = text.splitlines()
    result: list[str] = []
    in_target = False

    for line in lines:
        m = _REPORT_RE.match(line)
        if m:
            sym = m.group(3).strip()
            if symbol in sym:
                in_target = True
                result = [line]  # start fresh for each matching entry
            elif in_target:
                break  # new top-level entry; we're done
        elif in_target:
            result.append(line)
            if len(result) >= n + 1:
                result.append("(truncated — use a larger n or narrow the symbol)")
                break

    if not result:
        return f"Symbol {symbol!r} not found in call graph data."
    return "\n".join(result)


# ── perf annotate ─────────────────────────────────────────────────────────────

# Instruction line: "  12.34 :   80ab:   mov    (%rax),%rbx"
#   pct field is a decimal or blank (blank = 0 samples).
#   addr is 1+ hex chars (short addresses like "80" are common in JIT'd code).
_ANNOT_INSTR_RE = re.compile(
    r"^\s*(?P<pct>\d+\.\d+)?\s*:\s+(?P<addr>[0-9a-f]+):\s+(?P<asm>.+)$"
)

# Lines that look like instructions but didn't match — used to detect
# unexpected format changes without silently swallowing hot samples.
_ANNOT_LOOKALIKE_RE = re.compile(r"^\s*[\d.]*\s*:\s+\S+:\s+\S")


def _parse_annotate(text: str) -> tuple[list[dict], list[str]]:
    """Parse `perf annotate --stdio` output into a numbered list of line dicts.

    Returns (lines, warnings).  warnings is non-empty when suspicious
    unmatched lines were found, which likely indicates a format mismatch.

    Each dict has:
      lineno: 1-based line number (stable reference for read_around)
      kind:   "instr" | "source"
      pct:    sample percentage (0.0 for source/blank lines)
      addr:   hex address string (instr only)
      asm:    disassembly text (instr only)
      raw:    original line text (used for faithful reproduction)
    """
    parsed: list[dict] = []
    suspicious: list[str] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        # Skip the "Percent |" header and "---" separator lines.
        if raw.startswith("Percent |") or raw.startswith("---"):
            continue

        m = _ANNOT_INSTR_RE.match(raw)
        if m:
            pct_str = m.group("pct") or ""
            pct = float(pct_str) if pct_str else 0.0
            parsed.append(
                {
                    "lineno": lineno,
                    "kind": "instr",
                    "pct": pct,
                    "addr": m.group("addr"),
                    "asm": m.group("asm").strip(),
                    "raw": raw,
                }
            )
        else:
            parsed.append(
                {
                    "lineno": lineno,
                    "kind": "source",
                    "pct": 0.0,
                    "raw": raw,
                }
            )
            if _ANNOT_LOOKALIKE_RE.match(raw):
                suspicious.append(raw.rstrip())

    warnings: list[str] = []
    n_instr = sum(1 for l in parsed if l["kind"] == "instr")

    if suspicious:
        examples = "; ".join(f'"{s[:60]}"' for s in suspicious[:3])
        warnings.append(
            f"{len(suspicious)} line(s) looked like instructions but failed to "
            f"parse — possible perf output format change. Examples: {examples}"
        )

    if n_instr == 0 and len(parsed) > 5:
        warnings.append(
            f"No instruction lines found in {len(parsed)}-line annotation output. "
            "The perf annotate format may have changed or the wrong symbol/DSO was specified."
        )

    return parsed, warnings


def _find_matching_symbols(perf_data: str, symbol: str, dso: str | None) -> list[str]:
    """Find symbols in the profile that contain the given substring."""
    args = [
        "perf",
        "report",
        "--stdio",
        "--no-header",
        "--no-children",
        "--symbol-filter",
        symbol,
        "-i",
        perf_data,
    ]
    if dso:
        args += ["--dso", dso]
    text = _run(args)
    matches: list[str] = []
    for line in text.splitlines():
        m = _REPORT_RE.match(line)
        if m:
            matches.append(m.group(3).strip())
    return matches


def _get_annotate_lines(
    perf_data: str, symbol: str, dso: str | None
) -> tuple[list[dict], list[str]]:
    args = ["perf", "annotate", "--stdio", "-s", symbol, "-i", perf_data]
    if dso:
        args += ["--dso", dso]
    text = _run(args)
    if not text.strip():
        candidates = _find_matching_symbols(perf_data, symbol, dso)
        if candidates:
            listing = "\n  ".join(candidates)
            raise RuntimeError(
                f"No exact match for symbol {symbol!r}. "
                f"Similar symbols in the profile:\n  {listing}"
            )
        raise RuntimeError(f"No annotation found for symbol {symbol!r}")
    return _parse_annotate(text)


def annotate(
    perf_data: str,
    symbol: str,
    dso: str | None = None,
    min_pct: float = 0.5,
    context: int = 8,
) -> dict:
    """Smart annotated disassembly for a symbol.

    Returns a dict with:
      symbol:           the queried symbol
      total_lines:      total line count — use as reference for read_around
      top_instructions: top 20 hottest instructions sorted by sample %
      hot_blocks:       contiguous regions containing instructions >= min_pct,
                        each expanded by ±context lines, sorted by peak heat

    Use perf_annotate_read_around to drill into any line range.

    min_pct:  minimum sample % to consider an instruction "hot" (default 0.5)
    context:  lines of context around each hot cluster (default 8)
    """
    lines, parse_warnings = _get_annotate_lines(perf_data, symbol, dso)
    total = len(lines)

    # Top 20 hottest instructions
    instr_lines = [l for l in lines if l["kind"] == "instr" and l["pct"] > 0]
    top_instrs = sorted(instr_lines, key=lambda l: l["pct"], reverse=True)[:20]

    # Find hot instruction indices (0-based into `lines`)
    hot_idx = {i for i, l in enumerate(lines) if l["pct"] >= min_pct}

    # Merge nearby hot clusters (merge if gap <= 2*context)
    clusters: list[tuple[int, int]] = []
    if hot_idx:
        seq = sorted(hot_idx)
        lo = hi = seq[0]
        for idx in seq[1:]:
            if idx <= hi + context * 2:
                hi = idx
            else:
                clusters.append((lo, hi))
                lo = hi = idx
        clusters.append((lo, hi))

    # Expand each cluster by ±context and render
    hot_blocks: list[dict] = []
    for c_lo, c_hi in clusters:
        blk_lo = max(0, c_lo - context)
        blk_hi = min(total - 1, c_hi + context)
        block_lines = lines[blk_lo : blk_hi + 1]
        content = "\n".join(f"{l['lineno']:5d}  {l['raw']}" for l in block_lines)
        hot_blocks.append(
            {
                "line_range": f"{lines[blk_lo]['lineno']}-{lines[blk_hi]['lineno']}",
                "peak_pct": max(l["pct"] for l in block_lines),
                "content": content,
            }
        )

    hot_blocks.sort(key=lambda b: b["peak_pct"], reverse=True)

    result: dict = {
        "symbol": symbol,
        "total_lines": total,
        "min_pct_threshold": min_pct,
        "top_instructions": [
            {
                "lineno": l["lineno"],
                "addr": l["addr"],
                "pct": l["pct"],
                "asm": l["asm"],
            }
            for l in top_instrs
        ],
        "hot_blocks": hot_blocks,
    }
    if parse_warnings:
        result["parse_warnings"] = parse_warnings
    return result


def annotate_read_around(
    perf_data: str,
    symbol: str,
    line: int,
    context: int = 30,
    dso: str | None = None,
) -> str:
    """Return ±context lines of annotated disassembly around a line number.

    line:    1-based line number as reported by perf_annotate's total_lines /
             top_instructions / hot_blocks fields
    context: lines before and after (default 30)

    Each output line is prefixed with its line number for further navigation.
    """
    lines, parse_warnings = _get_annotate_lines(perf_data, symbol, dso)
    total = len(lines)
    center = line - 1  # convert to 0-based
    if not (0 <= center < total):
        raise ValueError(f"Line {line} out of range (1–{total})")
    lo = max(0, center - context)
    hi = min(total - 1, center + context)
    output = "\n".join(f"{l['lineno']:5d}  {l['raw']}" for l in lines[lo : hi + 1])
    if parse_warnings:
        header = "\n".join(f"[parse warning] {w}" for w in parse_warnings)
        output = header + "\n" + output
    return output


# ── perf diff ─────────────────────────────────────────────────────────────────

# Matches diff output lines with optional delta and optional DSO columns:
#   "    25.05%             libv8.so  [.] sym"   <- only in baseline (with DSO)
#   "     5.00%    -1.20%  libv8.so  [.] sym"   <- changed (with DSO)
#   "     5.00%    -1.20%  [.] sym"              <- changed (no DSO, --sort=symbol)
#   "              +3.45%  [.] sym"              <- new in after
_DIFF_RE = re.compile(
    r"^\s*([\d.]+%|)\s+([-+][\d.]+%|)\s+(?:(?!\[.\])(\S+)\s+)?\[.\]\s+(.+)$"
)


def diff(
    perf_before: str,
    perf_after: str,
    dso: str | None = None,
    n: int = 30,
) -> list[dict]:
    """Compare two perf profiles. Returns top N changes sorted by |delta_pct|.

    Each entry has:
      symbol:       function name
      dso:          shared object
      baseline_pct: self% in the before profile (None if absent)
      after_pct:    self% in the after profile (None if absent)
      delta_pct:    after_pct - baseline_pct (positive = got hotter)
    """
    # Sort by symbol only (not dso) so that JIT functions with PID-tagged DSO
    # names (e.g. jitted-115625-22.so) are matched across runs by name.
    args = ["perf", "diff", "--sort=symbol", perf_before, perf_after]
    if dso:
        args += ["--dso", dso]
    text = _run(args)

    rows: list[dict] = []
    for line in text.splitlines():
        m = _DIFF_RE.match(line)
        if not m:
            continue
        baseline_str = m.group(1).rstrip("%")
        delta_str = m.group(2).rstrip("%")
        dso_name = m.group(3) or ""
        sym = m.group(4).strip()

        # Normalize V8 JIT symbols: strip transient source location suffix
        # "JS:*foo (script.js):123:45" -> "JS:*foo"
        # "Builtin:Foo (d8):12:3"      -> "Builtin:Foo"
        sym = re.sub(r"\s+\([^)]+\):\d+:\d+$", "", sym)
        # Collapse PID-tagged JIT DSOs: "jitted-115625-22.so" -> "v8_jit"
        if re.match(r"jitted-\d+-\d+\.so$", dso_name):
            dso_name = "v8_jit"

        baseline = float(baseline_str) if baseline_str else None
        delta = float(delta_str) if delta_str else None

        if baseline is None and delta is None:
            continue

        after = None
        if baseline is not None and delta is not None:
            after = round(baseline + delta, 3)
        elif delta is not None:
            after = delta  # new symbol, baseline was 0

        rows.append(
            {
                "symbol": sym,
                "dso": dso_name,
                "baseline_pct": baseline,
                "after_pct": after,
                "delta_pct": delta,
            }
        )

    rows.sort(key=lambda r: abs(r["delta_pct"] or 0), reverse=True)
    return rows[:n]


# ── perf flamegraph ────────────────────────────────────────────────────────────

# Matches call-graph branch lines in `perf report -g callee --stdio` output:
#   "           --80.00%-- NativeRegExpExec"
#   "           |           --60.00%-- malloc"
_CG_BRANCH_RE = re.compile(r"^([\s|]*)--(\d+\.\d+)%--\s*(.+?)\s*$")


def _parse_cg_paths(
    block_lines: list[str],
    root_sym: str,
    root_pct: float,
    max_depth: int,
    absolute_pcts: bool = False,
) -> list[tuple[float, list[str]]]:
    """DFS a callee call-graph block into (abs_pct, path) leaf entries.

    When absolute_pcts is False (default, --no-children mode), branch
    percentages are relative to the parent and are multiplied down the tree.
    When True (--children mode), branch percentages are already absolute
    percentages of total samples and are used directly.
    """
    branches: list[tuple[int, str, float]] = []
    for line in block_lines:
        m = _CG_BRANCH_RE.match(line)
        if m:
            indent = len(m.group(1))
            pct_val = float(m.group(2))
            sym = m.group(3).strip()
            branches.append((indent, sym, pct_val))

    if not branches:
        return [(root_pct, [root_sym])]

    # Build tree using an indent-based parent stack.
    root_node: dict = {"sym": root_sym, "pct": root_pct, "children": []}
    stack: list[tuple[int, dict]] = [(-1, root_node)]

    for indent, sym, pct_val in branches:
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if absolute_pcts:
            node_pct = pct_val
        else:
            node_pct = parent["pct"] * pct_val / 100.0
        node: dict = {"sym": sym, "pct": node_pct, "children": []}
        parent["children"].append(node)
        stack.append((indent, node))

    # DFS → collect leaf paths (or paths at max_depth).
    results: list[tuple[float, list[str]]] = []

    def dfs(node: dict, path: list[str], depth: int) -> None:
        cur = path + [node["sym"]]
        if not node["children"] or depth >= max_depth:
            results.append((node["pct"], cur))
        else:
            for child in node["children"]:
                dfs(child, cur, depth + 1)

    dfs(root_node, [], 0)
    return results


def flamegraph(
    perf_data: str,
    focus_symbol: str | None = None,
    dso: str | None = None,
    min_pct: float = 0.5,
    depth: int = 8,
) -> str:
    """Aggregate hot call paths into a single text flamegraph view.

    Runs perf report in callee call-graph mode, parses each top-level
    symbol's subtree, and emits root→leaf paths sorted by absolute sample
    percentage.  Each path represents a distinct hot call chain.

    focus_symbol: restrict to call trees whose root matches this substring
    dso:          restrict to a specific shared object, e.g. "libv8.so"
    min_pct:      omit paths below this % of total samples (default 0.5)
    depth:        maximum call-chain depth to expand (default 8)
    """
    # When focus_symbol is set, use --children so the top-level percentage
    # is inclusive (total) time and call-graph branch percentages are absolute
    # (% of total samples).  Without focus_symbol, use --no-children for
    # self-time ordering (like perf_hotspots but with callee-tree context).
    use_children = focus_symbol is not None
    args = [
        "perf",
        "report",
        "--stdio",
        "--no-header",
        # callee mode: tree extends downward (what does this symbol call?)
        # use 0.01 as perf's internal threshold; we filter by min_pct in Python
        "-g",
        "callee,0.01,caller",
        "-i",
        perf_data,
    ]
    if not use_children:
        args.append("--no-children")
    if dso:
        args += ["--dso", dso]
    if focus_symbol:
        args += ["--symbol-filter", focus_symbol]
    text = _run(args)

    # Split perf output into per-symbol blocks and parse each one.
    all_paths: list[tuple[float, list[str]]] = []
    current_sym: str | None = None
    current_pct: float = 0.0
    block_lines: list[str] = []

    def flush() -> None:
        if current_sym is None:
            return
        if focus_symbol and focus_symbol not in current_sym:
            return
        for pct, path in _parse_cg_paths(
            block_lines, current_sym, current_pct, depth, absolute_pcts=use_children
        ):
            if pct >= min_pct:
                all_paths.append((pct, path))

    for line in text.splitlines():
        m = _REPORT_RE.match(line)
        if m:
            flush()
            current_pct = float(m.group(1))
            current_sym = m.group(3).strip()
            block_lines = []
        elif current_sym is not None:
            block_lines.append(line)
    flush()

    if not all_paths:
        msg = "No call paths found"
        if focus_symbol:
            msg += f" for {focus_symbol!r}"
        return msg + f" above {min_pct}%."

    # Deduplicate and sort by descending percentage.
    seen: set[tuple[str, ...]] = set()
    unique: list[tuple[float, list[str]]] = []
    for pct, path in sorted(all_paths, key=lambda x: x[0], reverse=True):
        key = tuple(path)
        if key not in seen:
            seen.add(key)
            unique.append((pct, path))

    return "\n".join(f"{pct:6.2f}%  {' > '.join(path)}" for pct, path in unique)


# ── perf tma ──────────────────────────────────────────────────────────────────

# Kernel PMU event names for Skylake-SP TMA Level 1 + L3 stall sub-metric.
# Recorded by linux-perf-d8.py --topdown as a single group.
_TMA_CORE_EVENTS = [
    "cycles",
    "topdown-total-slots",
    "topdown-fetch-bubbles",  # Frontend Bound numerator
    "topdown-slots-issued",  # Bad Speculation + Retiring numerator
    "topdown-slots-retired",  # Retiring numerator
    # topdown-recovery-bubbles (INT_MISC.RECOVERY_CYCLES) not available on
    # Skylake-SP — returns EINVAL.  Probed opportunistically so it works if
    # present on other hardware; bad_spec falls back to issued - retired.
    "topdown-recovery-bubbles",
]
_TMA_MEM_EVENT = "cycle_activity.stalls_l3_miss"  # Level 2: memory-bound

_TMA_RECORD_HINT = (
    "Re-record with linux-perf-d8.py --topdown to enable microarchitecture analysis."
)


def _evlist(perf_data: str) -> list[str]:
    """Return event names recorded in a perf.data file."""
    try:
        text = _run(["perf", "evlist", "-i", perf_data])
        return [l.strip() for l in text.splitlines() if l.strip()]
    except RuntimeError:
        return []


def _probe_event(perf_data: str, event: str) -> dict[str, float] | None:
    """Return {symbol: overhead_pct} for one event, or None if not recorded.

    In system-wide (-a) group recordings perf may use a 'dummy' sampling
    trigger, making the real events counting-only.  perf report still accepts
    --event=<name> for counting events in a group and returns the weighted
    per-symbol distribution, but the event name in the file must match exactly.
    We resolve the name via perf evlist before querying.
    """
    # Resolve the event name as it actually appears in the file (handles
    # aliases like cycles vs cpu-cycles, and :u/:S suffixes added by perf).
    recorded = _evlist(perf_data)
    matched = next((e for e in recorded if event in e or e in event), None)
    if matched is None:
        return None

    args = [
        "perf",
        "report",
        "--stdio",
        "--no-header",
        "--no-children",
        f"--event={matched}",
        "-i",
        perf_data,
    ]
    try:
        text = _run(args)
    except RuntimeError:
        return None
    result = {sym: pct for sym, (pct, _) in _parse_flat_report(text).items()}
    return result if result else None


def tma(
    perf_data: str,
    symbol: str | None = None,
    n: int = 20,
) -> dict:
    """Per-symbol TMA Level 1 breakdown using Skylake-SP topdown PMU events.

    Probes the perf.data for topdown events.  Always returns a dict with an
    'available' key so callers can handle both cases gracefully:

      available=False  →  only 'message' present; explains how to re-record
      available=True   →  'symbols' list with per-symbol TMA breakdown

    Intensity fields are event_pct / cycles_pct — how much of each event
    type this symbol attracts relative to its share of cycle time:
      > 1.0  more of this event than profile average (notable)
      ~ 1.0  proportional
      < 1.0  less than average

    Each symbol entry:
      cycles_pct:         % of cycle samples on this symbol (hotness)
      fe_intensity:       topdown-fetch-bubbles / cycles  (Frontend Bound)
      retiring_intensity: topdown-slots-retired / cycles  (Retiring / efficient)
      bad_spec_intensity: (slots-issued - slots-retired) / cycles  (Bad Spec)
      mem_intensity:      cycle_activity.stalls_l3_miss / cycles  (Memory Bound)
                          — only present when recorded with --topdown
      dominant:           "Frontend Bound" | "Backend Bound (Memory)" |
                          "Backend Bound (Core)" | "Bad Speculation" |
                          "Retiring (efficient)" | "Mixed"

    symbol:  filter to symbols containing this substring
    n:       max symbols to return, sorted by cycles_pct (default 20)
    """
    event_data: dict[str, dict[str, float]] = {}
    for ev in _TMA_CORE_EVENTS + [_TMA_MEM_EVENT]:
        result = _probe_event(perf_data, ev)
        if result is not None:
            event_data[ev] = result

    # Need at minimum: cycles + one topdown event to be useful.
    if "cycles" not in event_data or "topdown-fetch-bubbles" not in event_data:
        return {
            "available": False,
            "events_found": list(event_data.keys()),
            "message": _TMA_RECORD_HINT,
        }

    cycles = event_data["cycles"]
    slots = event_data.get("topdown-total-slots", cycles)  # fallback to cycles
    fe_bub = event_data.get("topdown-fetch-bubbles", {})
    issued = event_data.get("topdown-slots-issued", {})
    retired = event_data.get("topdown-slots-retired", {})
    recovery = event_data.get("topdown-recovery-bubbles", {})
    l3stall = event_data.get(_TMA_MEM_EVENT)  # None if not recorded

    syms = sorted(cycles, key=lambda s: cycles[s], reverse=True)
    if symbol:
        syms = [s for s in syms if symbol in s]
    syms = syms[:n]

    rows: list[dict] = []
    for sym in syms:
        cyc = cycles[sym]
        if not cyc:
            continue

        # Intensity = share of this event on symbol / share of cycles on symbol.
        # All topdown events are normalised against total-slots (≈ 4×cycles);
        # since we're computing ratios, using cycles_pct as denominator is
        # equivalent and avoids a separate total-slots lookup per symbol.
        fe_int = round(fe_bub.get(sym, 0.0) / cyc, 2)
        retiring_int = round(retired.get(sym, 0.0) / cyc, 2)
        # Bad Speculation: wasted issue slots + recovery overhead.
        # Uses recovery-bubbles when available (system-wide recording),
        # falls back to issued - retired when recorded per-thread.
        bad_spec_int = round(
            max(
                issued.get(sym, 0.0) - retired.get(sym, 0.0) + recovery.get(sym, 0.0),
                0.0,
            )
            / cyc,
            2,
        )
        mem_int = round(l3stall[sym] / cyc, 2) if l3stall and sym in l3stall else None

        # Classify dominant bottleneck.
        if fe_int >= 1.3:
            dominant = "Frontend Bound"
        elif bad_spec_int >= 0.5:
            dominant = "Bad Speculation"
        elif mem_int is not None and mem_int >= 1.3:
            dominant = "Backend Bound (Memory)"
        elif retiring_int >= 1.2 and fe_int < 0.8:
            dominant = "Retiring (efficient)"
        elif fe_int < 0.8 and bad_spec_int < 0.3:
            dominant = "Backend Bound (Core)"
        else:
            dominant = "Mixed"

        entry: dict = {
            "symbol": sym,
            "cycles_pct": cyc,
            "fe_intensity": fe_int,
            "retiring_intensity": retiring_int,
            "bad_spec_intensity": bad_spec_int,
            "dominant": dominant,
        }
        if mem_int is not None:
            entry["mem_intensity"] = mem_int
        rows.append(entry)

    return {
        "available": True,
        "events_recorded": list(event_data.keys()),
        "has_mem_detail": l3stall is not None,
        "symbols": rows,
    }
