# perf MCP tools — design notes

## Overview

Six MCP tools expose Linux `perf` analysis to an LLM.  All tools invoke the
`perf` binary via subprocess and parse its `--stdio` output; no perf Python
bindings or special kernel configuration is required beyond a normal
`perf record` capture.

---

## Typical workflow

```
perf_hotspots          ← find what to look at
  └── perf_callers     ← understand why it's hot (call graph)
  └── perf_annotate    ← see where inside the function time is spent
        └── perf_annotate_read_around   ← explore surrounding code
```

For before/after comparisons: `perf_diff` replaces `perf_hotspots` as the
entry point.

---

## Tool reference

### `perf_stat(stat_file)`

Parses a saved `perf stat` output file into structured counter data.  Useful
as context before diving into hotspots — IPC, cache miss rate, and branch
misprediction rate explain *why* code is slow, not just *where*.

**Input**: text file from `perf stat -o <file>` or `2>` redirection.

**Output**:
```json
{
  "elapsed_seconds": 4.321,
  "counters": [
    {"counter": "branch-misses",  "value": 1234567, "note": "2.34% of all branches"},
    {"counter": "cache-misses",   "value":  987654, "note": "12.34% of all cache refs"},
    {"counter": "cycles",         "value": 9876543210, "note": "3.21 GHz"},
    {"counter": "instructions",   "value": 8765432100, "note": "0.89 insn per cycle"},
    ...
  ]
}
```

---

### `perf_hotspots(perf_data, dso=None, n=30)`

Flat profile: top N symbols by **self%** (exclusive time), with **total%**
(inclusive time including callees) alongside.  The universal starting point.

Use `dso` to filter noise — e.g. `dso="d8"` to see only V8/d8 symbols and
exclude kernel and libc.

**Implementation**: runs `perf report --stdio --no-children` for self% and
`perf report --stdio` (with children) for total%, then merges the two by
symbol name.

**Output**: list of `{symbol, dso, self_pct, total_pct}` sorted by `self_pct`
descending.

---

### `perf_callers(perf_data, symbol, n=20)`

Call graph above a symbol: who calls it and with what sample weight.  Uses
caller-mode call graphs (`-g caller`) so the tree reads upward — direct
callers are nearest to the root.

Use this when `total_pct >> self_pct` for a symbol: the time is being charged
to it from callees, but the real hotspot may be a specific call site.

**Output**: raw perf-report call-graph text for the matching symbol, with line
count capped at `n`.

---

### `perf_annotate(perf_data, symbol, dso=None, min_pct=0.5, context=8)`

Annotated disassembly for a symbol, with smart extraction of the relevant
parts.  Raw `perf annotate` output can be thousands of lines for complex
functions; this tool surfaces the hottest regions first.

**Output**:
```json
{
  "symbol": "v8::internal::Foo::Bar",
  "total_lines": 847,
  "min_pct_threshold": 0.5,
  "top_instructions": [
    {"lineno": 312, "addr": "5612ab3c", "pct": 18.4, "asm": "mov (%rax,%rbx,8),%rcx"},
    {"lineno": 298, "addr": "5612ab28", "pct": 11.2, "asm": "cmp    %rdx,%rcx"},
    ...
  ],
  "hot_blocks": [
    {
      "line_range": "304-325",
      "peak_pct": 18.4,
      "content": "  304    12.34  :   5612ab30:  test   %rax,%rax\n  305  ..."
    },
    ...
  ]
}
```

**Algorithm**:
1. Parse all annotated lines into `{lineno, pct, addr, asm}` structs.
2. Select instructions with `pct >= min_pct` as "hot".
3. Merge nearby hot instructions into clusters (merge if gap ≤ `2 * context`).
4. Expand each cluster by `±context` lines to include surrounding context.
5. Return clusters sorted by peak heat, plus a global top-20 by instruction.

`total_lines` and the `lineno` fields are stable references for
`perf_annotate_read_around`.

---

### `perf_annotate_read_around(perf_data, symbol, line, context=30, dso=None)`

Read a `±context` line window of annotated disassembly centred on `line`.
Use this to explore code around a hot region identified by `perf_annotate`.

Each output line is prefixed with its line number:
```
  298    11.20  :   5612ab28:  cmp    %rdx,%rcx
  299     0.00  :   5612ab2b:  jge    5612ab50
  300     0.00  :   5612ab2d:  lea    0x10(%rbx),%rcx
  ...
```

---

### `perf_diff(perf_before, perf_after, dso=None, n=30)`

Compare two perf profiles side by side.  Returns the top N symbols sorted
by `|delta_pct|` — the biggest changes first, regardless of direction.

`delta_pct > 0` means the symbol got hotter in `perf_after`; `< 0` means
it got cooler.

**Implementation**: runs `perf diff --stdio` and parses the three-column
output (baseline%, delta%, symbol).

**Output**: list of `{symbol, dso, baseline_pct, after_pct, delta_pct}`.

---

## LLM usage notes

- **Start with `perf_hotspots`** (or `perf_diff` for before/after).  Pass
  `dso=` to filter out kernel and runtime noise when analysing a specific
  binary.

- **`perf_annotate` is the workhorse.**  `top_instructions` gives the quick
  answer; `hot_blocks` gives context.  `total_lines` tells you how much of
  the function you haven't seen yet.

- **Use `perf_annotate_read_around` to navigate.**  The `lineno` fields in
  `perf_annotate` output are stable references — call `read_around` with a
  line number from `top_instructions` or a `hot_blocks` boundary to explore
  surrounding code, preceding loops, or branch targets.

- **`perf_callers` bridges hotspots and call sites.**  When `total_pct` is
  much larger than `self_pct`, call `perf_callers` to find which call path
  is driving the inclusve cost.

- **`perf_stat` provides the physical picture.**  Low IPC + high cache misses
  suggests memory latency; high branch misprediction suggests speculative
  execution pressure.  Read this alongside annotation to form hypotheses.

---

## Limitations & known issues

- Symbol names must be exact matches for `perf annotate -s`; use
  `perf_hotspots` to get the exact name first.
- JIT-compiled code (V8 builtins built into the binary) annotates well.
  Dynamically generated JIT code requires `perf inject --jit` preprocessing
  before using these tools.
- `perf diff` output format varies across perf versions; the parser targets
  the format produced by perf 6.x.
