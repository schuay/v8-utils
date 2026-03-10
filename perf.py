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
            r'^\s+([\d,]+(?:\.\d+)?)\s+(?:msec\s+)?(\S.*?\S)\s{2,}(?:#\s+(.*))?$',
            line,
        )
        if m:
            raw = m.group(1).replace(",", "")
            try:
                value = float(raw)
            except ValueError:
                continue
            counters.append({
                "counter": m.group(2).strip(),
                "value":   value,
                "note":    m.group(3).strip() if m.group(3) else None,
            })
            continue

        m2 = re.match(r"^\s+([\d.]+)\s+seconds time elapsed", line)
        if m2:
            elapsed = float(m2.group(1))

    counters.sort(key=lambda c: c["counter"])
    return {"elapsed_seconds": elapsed, "counters": counters}


# ── perf report (flat profile) ────────────────────────────────────────────────

# Matches:  "    12.34%  d8  libv8.so  [.] v8::Foo::Bar<int>"
_REPORT_RE = re.compile(r"^\s*([\d.]+)%\s+\S+\s+(\S+)\s+\[.\]\s+(.+)$")


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
    self_data  = _parse_flat_report(_run(base + ["--no-children"]))
    total_data = _parse_flat_report(_run(base))

    top = sorted(self_data.items(), key=lambda x: x[1][0], reverse=True)[:n]
    return [
        {
            "symbol":    sym,
            "dso":       sym_dso,
            "self_pct":  self_pct,
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
        "perf", "report", "--stdio", "--no-header",
        "-g", "caller,0.01,callee",
        "--no-children",
        "-i", perf_data,
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
                result = [line]       # start fresh for each matching entry
            elif in_target:
                break                  # new top-level entry; we're done
        elif in_target:
            result.append(line)
            if len(result) >= n + 1:
                result.append("(truncated — use a larger n or narrow the symbol)")
                break

    if not result:
        return f"Symbol {symbol!r} not found in call graph data."
    return "\n".join(result)


# ── perf annotate ─────────────────────────────────────────────────────────────

# Instruction line: optional leading percentage, colon, hex address, colon, asm
# Examples:
#   "  12.34  :   1234ab:   mov    (%rax),%rbx"
#   "         :   1234ab:   push   %rbp"          <- 0%, blank pct field
_ANNOT_INSTR_RE = re.compile(
    r"^(?P<pct>[ \d]*(?:\.\d+)?)?\s*:\s+(?P<addr>[0-9a-f]{4,}):\s+(?P<asm>.+)$"
)


def _parse_annotate(text: str) -> list[dict]:
    """Parse `perf annotate --stdio` output into a numbered list of line dicts.

    Each dict has:
      lineno: 1-based line number (stable reference for read_around)
      kind:   "instr" | "source"
      pct:    sample percentage (0.0 for source/blank lines)
      addr:   hex address string (instr only)
      asm:    disassembly text (instr only)
      raw:    original line text (used for faithful reproduction)
    """
    parsed: list[dict] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        m = _ANNOT_INSTR_RE.match(raw)
        if m:
            pct_str = m.group("pct") or ""
            pct = float(pct_str.strip()) if pct_str.strip() else 0.0
            parsed.append({
                "lineno": lineno,
                "kind":   "instr",
                "pct":    pct,
                "addr":   m.group("addr"),
                "asm":    m.group("asm").strip(),
                "raw":    raw,
            })
        else:
            parsed.append({
                "lineno": lineno,
                "kind":   "source",
                "pct":    0.0,
                "raw":    raw,
            })
    return parsed


def _get_annotate_lines(
    perf_data: str, symbol: str, dso: str | None
) -> list[dict]:
    args = ["perf", "annotate", "--stdio", "--no-header",
            "-s", symbol, "-i", perf_data]
    if dso:
        args += ["--dso", dso]
    text = _run(args)
    if not text.strip():
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
    lines = _get_annotate_lines(perf_data, symbol, dso)
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
        hot_blocks.append({
            "line_range": f"{lines[blk_lo]['lineno']}-{lines[blk_hi]['lineno']}",
            "peak_pct":   max(l["pct"] for l in block_lines),
            "content":    content,
        })

    hot_blocks.sort(key=lambda b: b["peak_pct"], reverse=True)

    return {
        "symbol":            symbol,
        "total_lines":       total,
        "min_pct_threshold": min_pct,
        "top_instructions": [
            {
                "lineno": l["lineno"],
                "addr":   l["addr"],
                "pct":    l["pct"],
                "asm":    l["asm"],
            }
            for l in top_instrs
        ],
        "hot_blocks": hot_blocks,
    }


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
    lines = _get_annotate_lines(perf_data, symbol, dso)
    total = len(lines)
    center = line - 1  # convert to 0-based
    if not (0 <= center < total):
        raise ValueError(f"Line {line} out of range (1–{total})")
    lo = max(0, center - context)
    hi = min(total - 1, center + context)
    return "\n".join(f"{l['lineno']:5d}  {l['raw']}" for l in lines[lo : hi + 1])


# ── perf diff ─────────────────────────────────────────────────────────────────

# Matches diff output lines with optional delta column:
#   "    25.05%             libv8.so  [.] sym"   <- only in baseline
#   "     5.00%    -1.20%  libv8.so  [.] sym"   <- changed
#   "              +3.45%  libv8.so  [.] sym"   <- new in after
_DIFF_RE = re.compile(
    r"^\s*([\d.]+%|)\s+([-+][\d.]+%|)\s+(\S+)\s+\[.\]\s+(.+)$"
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
    args = ["perf", "diff", "--stdio", "--no-header", perf_before, perf_after]
    if dso:
        args += ["--dso", dso]
    text = _run(args)

    rows: list[dict] = []
    for line in text.splitlines():
        m = _DIFF_RE.match(line)
        if not m:
            continue
        baseline_str = m.group(1).rstrip("%")
        delta_str    = m.group(2).rstrip("%")
        dso_name     = m.group(3)
        sym          = m.group(4).strip()

        baseline = float(baseline_str) if baseline_str else None
        delta    = float(delta_str)    if delta_str    else None

        if baseline is None and delta is None:
            continue

        after = None
        if baseline is not None and delta is not None:
            after = round(baseline + delta, 3)
        elif delta is not None:
            after = delta  # new symbol, baseline was 0

        rows.append({
            "symbol":       sym,
            "dso":          dso_name,
            "baseline_pct": baseline,
            "after_pct":    after,
            "delta_pct":    delta,
        })

    rows.sort(key=lambda r: abs(r["delta_pct"] or 0), reverse=True)
    return rows[:n]
