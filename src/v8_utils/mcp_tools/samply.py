"""MCP tools for reading samply recordings (macOS profiles from jsb_run_bench
record="perf")."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from ..api import perf as perf_tools
from ..api import samply
from ..render import samply as render
from ._shared import _text_result

SUMMARY = (
    "samply_* (macOS samply recordings: hotspots, callers, flamegraph, diff, annotate)"
)

# samply_hotspots remembers its most recent rows per recording so that later
# calls can name a function as "#3".
_symbols = perf_tools.SymbolCache()

RECORDING_ARG = (
    'samply recording directory (as returned by jsb_run_bench record="perf"),'
    " or its profile.json.gz"
)
THREAD_ARG = (
    "thread name or tid (default: the thread with the most CPU time, the d8"
    " main thread); see samply_threads"
)
SYMBOL_ARG = "function name, unique substring, or #N from samply_hotspots"
WALL_ARG = (
    "weigh samples by elapsed time instead of CPU time, so blocked time"
    " (waits, I/O) counts"
)


def _thread(thread: str | None) -> str | int | None:
    return int(thread) if thread is not None and thread.isdigit() else thread


def _resolve(recording: str, symbol: str) -> str:
    return _symbols.resolve(recording, symbol)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def samply_hotspots(
        recording: Annotated[str, Field(description=RECORDING_ARG)],
        thread: Annotated[str | None, Field(description=THREAD_ARG)] = None,
        n: Annotated[int, Field(description="number of functions to return")] = 30,
        wall: Annotated[bool, Field(description=WALL_ARG)] = False,
        merge_tiers: Annotated[
            bool,
            Field(
                description=(
                    "count a JS function's Ignition/Sparkplug/Maglev/Turbofan code"
                    " as one row"
                )
            ),
        ] = False,
        by_lib: Annotated[
            bool, Field(description="aggregate per library instead of per function")
        ] = False,
        filter: Annotated[
            str | None, Field(description="only functions whose name contains this")
        ] = None,
    ) -> CallToolResult:
        """Top functions of a samply recording by self time, with total
        (inclusive) time, frame kind (js, builtin, regexp, native), JS tier
        and library.

        JS rows are per code object flavour: tier, "osr" for on-stack
        replacement code and "ctx" for context-specialized code.

        Typical workflow: samply_hotspots -> samply_callers / samply_flamegraph
        -> samply_annotate.
        """
        profile = samply.load(recording)
        h = samply.hotspots(
            profile,
            _thread(thread),
            n=n,
            wall=wall,
            merge_tiers=merge_tiers,
            by_lib=by_lib,
            filter=filter,
        )
        if not by_lib:
            _symbols.remember(recording, [{"symbol": r.function.name} for r in h.rows])
        return _text_result(render.format_hotspots(profile, h))

    @mcp.tool()
    def samply_callers(
        recording: Annotated[str, Field(description=RECORDING_ARG)],
        symbol: Annotated[str, Field(description=SYMBOL_ARG)],
        thread: Annotated[str | None, Field(description=THREAD_ARG)] = None,
        depth: Annotated[int, Field(description="callers to show per path")] = 5,
        n: Annotated[int, Field(description="number of paths to return")] = 20,
        wall: Annotated[bool, Field(description=WALL_ARG)] = False,
    ) -> CallToolResult:
        """Who calls a function: the distinct caller chains into it, nearest
        caller first, weighted by the time spent in and under it.

        Use this when total% >> self% in samply_hotspots, or to see which call
        site makes a builtin or runtime function hot.
        """
        c = samply.callers(
            samply.load(recording),
            _resolve(recording, symbol),
            _thread(thread),
            depth=depth,
            n=n,
            wall=wall,
        )
        return _text_result(render.format_callers(c))

    @mcp.tool()
    def samply_flamegraph(
        recording: Annotated[str, Field(description=RECORDING_ARG)],
        focus: Annotated[
            str | None,
            Field(
                description=(
                    "show only the subtree under this function (name, substring or"
                    " #N from samply_hotspots), rooted at it"
                )
            ),
        ] = None,
        thread: Annotated[str | None, Field(description=THREAD_ARG)] = None,
        depth: Annotated[int, Field(description="frames per path, from the root")] = 30,
        min_pct: Annotated[
            float, Field(description="omit paths below this % of the thread's time")
        ] = 0.5,
        wall: Annotated[bool, Field(description=WALL_ARG)] = False,
        native_root: Annotated[
            bool,
            Field(
                description=(
                    "keep the native frames above the outermost JS/builtin frame"
                    " (process start, d8's message loop), which are hidden by"
                    " default"
                )
            ),
        ] = False,
    ) -> CallToolResult:
        """Collapsed stacks: every hot root-to-leaf path with its share of the
        thread's time, heaviest first ("a;b;c <ms>").

        With focus, paths start at that function, which shows where its
        inclusive time goes.
        """
        if focus is not None:
            focus = _resolve(recording, focus)
        fg = samply.flamegraph(
            samply.load(recording),
            _thread(thread),
            focus=focus,
            depth=depth,
            min_pct=min_pct,
            wall=wall,
            native_root=native_root,
        )
        return _text_result(render.format_flamegraph(fg))

    @mcp.tool()
    def samply_diff(
        baseline: Annotated[str, Field(description="baseline samply recording")],
        experiment: Annotated[str, Field(description="experiment samply recording")],
        thread: Annotated[str | None, Field(description=THREAD_ARG)] = None,
        n: Annotated[int, Field(description="number of functions to return")] = 30,
        wall: Annotated[bool, Field(description=WALL_ARG)] = False,
        merge_tiers: Annotated[
            bool,
            Field(
                description=(
                    "compare a JS function across tiers as one row (default), so"
                    " code that tiered up differently still lines up"
                )
            ),
        ] = True,
    ) -> CallToolResult:
        """Compare two samply recordings: per-function self time as a share of
        each recording's thread time, biggest change first. "new" and "gone"
        mark functions present in only one recording."""
        rows = samply.diff(
            samply.load(baseline),
            samply.load(experiment),
            _thread(thread),
            n=n,
            wall=wall,
            merge_tiers=merge_tiers,
        )
        return _text_result(render.format_diff(rows))

    @mcp.tool()
    def samply_annotate(
        recording: Annotated[str, Field(description=RECORDING_ARG)],
        symbol: Annotated[str, Field(description=SYMBOL_ARG)],
        code: Annotated[
            int | None,
            Field(
                description=(
                    "which code object to annotate, by its number in the listed"
                    " code objects (default: the hottest)"
                )
            ),
        ] = None,
        thread: Annotated[str | None, Field(description=THREAD_ARG)] = None,
        min_pct: Annotated[
            float, Field(description="minimum % of the code object to count as hot")
        ] = 0.5,
        context: Annotated[
            int, Field(description="lines of context around each hot cluster")
        ] = 8,
        wall: Annotated[bool, Field(description=WALL_ARG)] = False,
    ) -> CallToolResult:
        """Annotated disassembly: self time per instruction of one code object
        of a function, with the hottest instructions and hot blocks (+/-context
        lines, sorted by peak heat).

        A JS function has one code object per tier and recompilation, all
        listed; JIT code is interleaved with its JS source positions
        ("; script:line:col", plus the source line when the script is on
        disk). Native code comes from the binary the recording used. Samples
        are exact PCs, so heat can sit one instruction after a slow load.

        Line numbers index the full listing: use samply_annotate_read_around
        to read more of it.
        """
        a = samply.annotate(
            samply.load(recording),
            _resolve(recording, symbol),
            _thread(thread),
            code=code,
            wall=wall,
        )
        return _text_result(
            render.format_annotation(a, min_pct=min_pct, context=context)
        )

    @mcp.tool()
    def samply_annotate_read_around(
        recording: Annotated[str, Field(description=RECORDING_ARG)],
        symbol: Annotated[str, Field(description=SYMBOL_ARG)],
        line: Annotated[int, Field(description="1-based listing line to centre on")],
        code: Annotated[
            int | None,
            Field(description="code object number, as passed to samply_annotate"),
        ] = None,
        context: Annotated[
            int, Field(description="lines before and after to include")
        ] = 30,
        thread: Annotated[str | None, Field(description=THREAD_ARG)] = None,
        wall: Annotated[bool, Field(description=WALL_ARG)] = False,
    ) -> CallToolResult:
        """A window of the samply_annotate listing around a line, for reading
        beyond the hot blocks."""
        a = samply.annotate(
            samply.load(recording),
            _resolve(recording, symbol),
            _thread(thread),
            code=code,
            wall=wall,
        )
        return _text_result(render.format_read_around(a, line, context=context))

    @mcp.tool()
    def samply_threads(
        recording: Annotated[str, Field(description=RECORDING_ARG)],
        top: Annotated[int, Field(description="hottest functions per thread")] = 3,
    ) -> CallToolResult:
        """Every thread in a samply recording with wall time, CPU time and
        CPU/wall ratio (low means blocked or idle), busiest first, plus its
        hottest functions. Use the tid as `thread` in the other samply_*
        tools; d8's background compiler and GC threads share one name."""
        return _text_result(
            render.format_threads(samply.threads(samply.load(recording), top=top))
        )
