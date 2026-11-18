"""MCP tools for reading Linux perf profiles."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from ..api import perf as perf_tools
from ._shared import _text_result

SUMMARY = "perf_* (Linux perf profiles)"

# perf_hotspots remembers its most recent result per perf_data path so that
# downstream tools can accept "#3" instead of the raw symbol name.
_symbols = perf_tools.SymbolCache()

# Argument documentation lives on the argument (Annotated[..., Field(...)]), so
# a client sends it as the parameter's own schema description instead of leaving
# the model to match a prose line against a signature by name. These four recur
# across the perf tools; the rest are inline at their parameter.
PERF_DATA_ARG = "path to a perf.data file"
DSO_ARG = 'restrict to a specific shared object, e.g. "libv8.so" or "d8"'
SYMBOL_ARG = "symbol name, unique substring, or #N from perf_hotspots"
CONTEXT_ARG = "lines of context around each hot cluster"


def _resolve_symbol(perf_data: str, symbol: str, **_kw: object) -> str:
    return _symbols.resolve(perf_data, symbol)


def register(mcp: FastMCP) -> None:
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
        _symbols.remember(perf_data, rows)
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
