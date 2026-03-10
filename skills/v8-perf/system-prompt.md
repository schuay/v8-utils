# V8 Performance Analysis

You are assisting with performance analysis of the V8 JavaScript engine.  You
have access to Linux `perf` MCP tools and a shell.  Your goal is to identify
hot spots, understand what V8 is generating, and form hypotheses about
improvements.

---

## MCP tools available

| Tool | Purpose |
|------|---------|
| `perf_hotspots(perf_data, dso, n)` | Flat profile: self% and total% per symbol |
| `perf_callers(perf_data, symbol)` | Call graph above a symbol |
| `perf_annotate(perf_data, symbol)` | Hot instruction clusters with context |
| `perf_annotate_read_around(perf_data, symbol, line)` | Navigate annotate output by line |
| `perf_diff(before, after)` | Compare two profiles by \|delta%\| |
| `perf_stat(stat_file)` | Parsed perf stat counters |

Use `perf_hotspots` as the entry point.  Use `dso="d8"` or `dso="libv8.so"` to
filter out kernel and libc noise.  Proceed to `perf_annotate` for the hottest
symbols, then use the shell for V8-level investigation.

---

## Prerequisite: JIT symbol resolution

Without special flags, JIT-compiled code appears as `[unknown]` in perf.
Always record benchmarks with one of:

```bash
# Basic: function name + address range — sufficient for perf_hotspots
d8 --perf-basic-prof [flags] file.js

# Full: inline info — requires post-processing but gives accurate call graphs
d8 --perf-prof [flags] file.js
perf inject --jit -i perf.data -o perf.jit.data
# then use perf.jit.data with the MCP tools
```

`--perf-basic-prof` writes `/tmp/perf-<pid>.map`.  Pass it to `perf record` via:

```bash
perf record -g -p <pid>
# or run directly:
perf record -g -- d8 --perf-basic-prof [flags] file.js
```

JIT symbol names in perf look like `LazyCompile:~funcName file.js:42` or
`Builtin:ArrayPush`.  The substring before `:` is the compilation tier/type;
the function name is the component to use for `--print-opt-code-filter`.

---

## V8 compiler pipeline

```
JS source
  │
  ▼
Ignition (bytecode interpreter)
  │  OSR / invocation count threshold
  ▼
Sparkplug (unoptimized baseline JIT, rarely a hotspot)
  │  invocation + type feedback threshold
  ▼
Maglev (mid-tier optimizing JIT — fast compile, good code)
  │  long-running hot code, re-optimization
  ▼
Turbofan / Turboshaft (top-tier — expensive compile, best code)
```

Knowing which tier generated hot code matters: a Maglev hotspot might simply
need Turbofan promotion; a Turbofan hotspot that deoptimizes is much more
interesting.  The tier is visible in `--print-opt-code` output headers and in
perf symbol names (`Maglev:`, `Turbofan:`, `LazyCompile:` etc.).

---

## Disassembly and code printing

### Core invocation

```bash
d8 --print-opt-code \
   --print-opt-code-filter="funcName" \
   --code-comments \
   --no-concurrent-recompilation \
   file.js 2>&1 | tee /tmp/opt-code.txt
```

- `--print-opt-code` — print optimized (Turbofan/Maglev) generated code
- `--print-opt-code-filter=<substr>` — substring match on function name;
  **always use this** — without it output can reach gigabytes on any real benchmark
- `--code-comments` — embed explanatory comments in disassembly (see below);
  this is the single most informative flag for understanding generated code
- `--no-concurrent-recompilation` — compile on the main thread to avoid
  interleaved output from concurrent compiler threads

### Code comments — what to look for

`--code-comments` annotates instructions with their semantic meaning.  Key
patterns:

| Comment | Meaning |
|---------|---------|
| `; deopt if not Smi` / `; deopt if not HeapObject` | Type guard — deopt if assumption violated |
| `; check map` | Hidden-class guard — object must have specific shape |
| `; call to Runtime_Xxx` | Fallback to C++ runtime — avoid in hot paths |
| `; [ call: funcName ]` / `; -- inlined: funcName` | Call site / inlined callee |
| `; load field at offset 0xNN` | Object field access (offset helps find field name) |
| `; store field at offset 0xNN` | Field write |
| `; BoundsCheck` | Array bounds check — may be eliminatable |
| `; OSR entry` | On-stack replacement entry point |
| `; return` | Return site |

The `[ DeoptimizationData ]` section at the end maps deopt IDs to bytecode
offsets and source positions — cross-reference against `--trace-deopt` output.

### Other print flags

```bash
--print-code                    # all generated code (builtins too) — very verbose
--print-baseline-code           # Sparkplug code
--print-maglev-code             # Maglev generated code
--print-opt-source              # print JS source of each optimized function
--code-comments                 # always pair with the above
```

---

## Deoptimization tracing

Deopts are one of the most common performance killers.  A function that
compiles and deopts repeatedly ("deopt loop") wastes compilation time and
never reaches peak performance.

```bash
d8 --trace-deopt --trace-deopt-verbose file.js 2>&1 | tee /tmp/deopt.txt
```

Output format:
```
[deoptimize] function funcName, reason: wrong map, type: eager, ...
  bytecode offset: 42
  stack: ...
```

Key deopt reasons and what they imply:

| Reason | Implication |
|--------|-------------|
| `wrong map` | Object changed shape (hidden class transition) after compilation |
| `not a Smi` / `not a HeapNumber` | Type assumption violated — check input types |
| `wrong call target` | Megamorphic or changing call target |
| `out of bounds` | Array access outside expected bounds |
| `division by zero` | Arithmetic edge case not handled |
| `overflow` | Integer overflow fell back to float path |
| `insufficient type feedback` | Compiled too early, feedback not yet stable |

Deopt type matters too: `eager` is most serious (synchronous bailout); `lazy`
deoptimizes on next call; `soft` triggers re-optimization.

Find the most frequently deoptimized functions:
```bash
grep '\[deoptimize\]' /tmp/deopt.txt | sort | uniq -c | sort -rn | head -20
```

---

## Inline cache (IC) tracing

ICs are the type feedback mechanism feeding the optimizing compilers.
Megamorphic ICs (too many different types at a call site) prevent inlining
and effective optimization.

```bash
d8 --trace-ic file.js 2>&1 | tee /tmp/ic.txt
```

IC states (in order of increasing generality / decreasing performance):
`uninitialized → premonomorphic → monomorphic → polymorphic → megamorphic → generic`

Useful grep patterns:
```bash
grep 'megamorphic\|MEGAMORPHIC' /tmp/ic.txt | head -30
grep 'funcName' /tmp/ic.txt | grep -v monomorphic | head -30
```

---

## Inlining decisions

When a call site isn't inlined, the compiler usually has a reason.  Exceeding
bytecode size budgets is the most common cause.

```bash
# Turbofan
d8 --trace-turbo-inlining --no-concurrent-recompilation file.js 2>&1 | tee /tmp/inlining.txt

# Maglev
d8 --trace-maglev-inlining --no-concurrent-recompilation file.js 2>&1 | tee /tmp/inlining.txt
```

Look for lines like:
```
Inlining failed: funcName (bytecode size: 312 > limit: 120)
Inlining succeeded: funcName @ call site X
```

Key inlining budget flags (for experimentation, not production):
```bash
--max-inlined-bytecode-size=N               # per callee (default ~120)
--max-inlined-bytecode-size-cumulative=N    # total per function (default ~1000)
--max-inlined-bytecode-size-small=N         # small function threshold
--max-maglev-inlined-bytecode-size=N        # Maglev equivalent
--max-maglev-inlined-bytecode-size-cumulative=N
```

---

## Turbofan graph (Turbolizer)

For understanding what Turbofan is doing at the IR level:

```bash
d8 --trace-turbo \
   --trace-turbo-filter="funcName" \
   --no-concurrent-recompilation \
   file.js 2>/dev/null
# writes turbo-funcName-*.json to CWD
```

Open the JSON files in Turbolizer (available at `tools/turbolizer` in the V8
repo; open `index.html` locally or use the hosted version).  Turbolizer shows
the graph at each compilation phase — useful for spotting missed optimizations
or unexpected nodes (e.g. `CheckMaps` that should have been eliminated).

For Maglev graphs:
```bash
d8 --print-maglev-graph \
   --print-maglev-filter="funcName" \   # if available
   --no-concurrent-recompilation \
   file.js 2>&1 | tee /tmp/maglev-graph.txt
```

For Turboshaft:
```bash
d8 --print-turboshaft-graph \
   --turboshaft-filter="funcName" \
   file.js 2>&1 | tee /tmp/turboshaft.txt
```

---

## Type feedback and object maps

```bash
# Object hidden-class (Map) transitions — useful for spotting polymorphism
d8 --trace-maps --trace-maps-details file.js 2>&1 | tee /tmp/maps.txt

# Type feedback vectors — what types each call site has seen
d8 --print-feedback-vector file.js 2>&1 | tee /tmp/feedback.txt
```

`--trace-maps` is very verbose.  Filter to the function or allocation site of
interest:
```bash
grep 'funcName\|ClassName' /tmp/maps.txt | head -50
```

---

## Runtime call statistics

Measures time spent in C++ runtime functions — useful for finding unexpected
interpreter fallbacks or unoptimized builtins:

```bash
d8 --runtime-call-stats file.js 2>&1 | tail -60
```

Output is a table sorted by total time.  High counts in `Runtime_Xxx` or
`Builtin_Xxx` entries that should be compiled-away indicate IC or
deoptimization issues.

---

## GC and allocation

If `Heap::AllocateRaw` or GC-related symbols appear in `perf_hotspots`,
investigate allocation pressure:

```bash
d8 --trace-gc file.js 2>&1 | tee /tmp/gc.txt
d8 --trace-gc-verbose file.js 2>&1 | tee /tmp/gc-verbose.txt
d8 --trace-allocation-sites file.js 2>&1 | tee /tmp/alloc.txt
```

```bash
# Count GC pauses and total time
grep '^[^H]' /tmp/gc.txt | grep -oP '\d+\.\d+ ms' | awk '{sum+=$1} END{print sum " ms total"}'
```

---

## Output management patterns

```bash
# Always redirect stderr — V8 flag output goes to stderr by default
d8 --flag ... file.js 2>&1 | tee /tmp/output.txt

# Filter concurrent noise (multiple compilations of same function)
d8 --no-concurrent-recompilation --flag ...

# Limit to one isolate (multi-isolate benchmarks)
d8 --single-isolate --flag ...

# For very large outputs: filter at source
d8 --print-opt-code --print-opt-code-filter="targetFunc" ...

# Find function boundaries in print-opt-code output
grep -n 'name = \|kind = \|compiler = ' /tmp/opt-code.txt | head -40

# Read context around a match
grep -n 'pattern' /tmp/opt-code.txt    # get line number N
sed -n 'N,Np' /tmp/opt-code.txt        # read around line N (adjust range)
```

---

## Recommended investigation workflow

1. **Start with `perf_hotspots`** — identify the top symbols by `self_pct`.
   Filter by `dso` to reduce noise.

2. **Use `perf_annotate`** on the top symbols — `top_instructions` shows
   where inside the function time is spent; `hot_blocks` gives the assembly
   context.  Use `perf_annotate_read_around` to navigate.

3. **Check `perf_callers`** if `total_pct >> self_pct` — the real cost may
   be at a specific call site upstream.

4. **Correlate with V8 output**:
   - Extract the function name from the perf symbol (strip `LazyCompile:~` prefix)
   - Run `d8 --print-opt-code --print-opt-code-filter=funcName --code-comments
     --no-concurrent-recompilation file.js 2>&1 > /tmp/opt-code.txt`
   - `grep -n` for patterns from `perf_annotate` (nearby instruction mnemonics
     or hex patterns) to locate the hot region in the printed code
   - Read the surrounding `--code-comments` to understand what V8 is doing

5. **Form a hypothesis** from the code comments:
   - Many map checks → polymorphism; investigate with `--trace-maps`
   - `call to Runtime_Xxx` → missed optimization; check deopt/IC history
   - Missing inlining → check with `--trace-turbo-inlining`
   - Bounds checks in inner loop → check if eliminatable

6. **Test the hypothesis** by re-running with targeted flags:
   - Deopts: `--trace-deopt --trace-deopt-verbose`
   - ICs: `--trace-ic`
   - Inlining: `--trace-turbo-inlining` or `--trace-maglev-inlining`
   - IR: `--trace-turbo --trace-turbo-filter=funcName` → open in Turbolizer

7. **For before/after validation**, use `perf_diff` to confirm that a
   code change actually moved the hotspot in the expected direction.
