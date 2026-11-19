"""MCP tools for reading samply recordings (macOS profiles from jsb_run_bench
record="perf")."""

from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from ..api import perf as perf_tools
from ..api import samply
from ..render import samply as render
from ._shared import _text_result

SUMMARY = (
    "samply_* (macOS samply recordings: hotspots, callers, flamegraph, diff,"
    " annotate, slices)"
)

# samply_hotspots remembers its most recent function rows per canonical profile
# path so that later calls can name a function as "#3".
_symbols = perf_tools.SymbolCache("samply_hotspots")

# samply_slices remembers its most recent named slices per canonical profile
# path so a custom min_iteration carries through to subsequent time="worst" calls.
_slice_cache: dict[str, dict[str, str]] = {}

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
MERGE_TIERS_ARG = (
    "count a JS function's Ignition/Sparkplug/Maglev/Turbofan (and osr/ctx)"
    " code flavours together"
)
TIME_ARG = (
    'restrict to time window(s): ms/s ranges ("100,250", "100,150 310,360",'
    ' "0.1s,0.3s"), percentages ("0%,20%"), or named iteration slices'
    ' ("first", "worst", "best", "average", "iters"); see samply_slices'
)


def _thread(thread: str | None) -> str | int | None:
    return int(thread) if thread is not None and thread.isdigit() else thread


def _symbol_key(profile: samply.Profile) -> str:
    return str(profile.path.resolve())


def _resolve(profile: samply.Profile, symbol: str) -> str:
    return _symbols.resolve(_symbol_key(profile), symbol)


def _resolve_time_arg(profile: samply.Profile, time: str | None) -> str | None:
    if time is None:
        return None
    cached = _slice_cache.get(_symbol_key(profile))
    if cached is not None:
        hit = cached.get(time.strip().lower())
        if hit is not None:
            return hit
    return time


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def samply_hotspots(
        recording: Annotated[str, Field(description=RECORDING_ARG)],
        thread: Annotated[str | None, Field(description=THREAD_ARG)] = None,
        n: Annotated[int, Field(description="number of functions to return")] = 30,
        wall: Annotated[bool, Field(description=WALL_ARG)] = False,
        merge_tiers: Annotated[
            bool,
            Field(description=MERGE_TIERS_ARG),
        ] = False,
        by_lib: Annotated[
            bool, Field(description="aggregate per library instead of per function")
        ] = False,
        filter: Annotated[
            str | None, Field(description="only functions whose name contains this")
        ] = None,
        sort_by: Annotated[
            Literal["self", "total"],
            Field(
                description=(
                    'rank by "self" (exclusive time, default) or "total"'
                    " (inclusive time, to surface dispatchers and wrappers)"
                )
            ),
        ] = "self",
        time: Annotated[str | None, Field(description=TIME_ARG)] = None,
    ) -> CallToolResult:
        """Top functions of a samply recording by self (or total) time, with
        total (inclusive) time, frame kind (js, builtin, regexp, native), JS
        tier and library.

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
            sort_by=sort_by,
            time=_resolve_time_arg(profile, time),
        )
        # Keep the last function-level #N listing intact when called with
        # by_lib=True (often in parallel with samply_hotspots(n=...)).
        if not by_lib:
            _symbols.remember(
                _symbol_key(profile),
                [{"symbol": r.function.name} for r in h.rows],
            )
        return _text_result(render.format_hotspots(profile, h))

    @mcp.tool()
    def samply_callers(
        recording: Annotated[str, Field(description=RECORDING_ARG)],
        symbol: Annotated[str, Field(description=SYMBOL_ARG)],
        thread: Annotated[str | None, Field(description=THREAD_ARG)] = None,
        depth: Annotated[int, Field(description="callers to show per path")] = 5,
        n: Annotated[int, Field(description="number of paths to return")] = 20,
        wall: Annotated[bool, Field(description=WALL_ARG)] = False,
        merge_tiers: Annotated[
            bool,
            Field(description=MERGE_TIERS_ARG),
        ] = False,
        time: Annotated[str | None, Field(description=TIME_ARG)] = None,
    ) -> CallToolResult:
        """Who calls a function: the distinct caller chains into it, nearest
        caller first, weighted by the time spent in and under it.

        Use this when total% >> self% in samply_hotspots, or to see which call
        site makes a builtin or runtime function hot.
        """
        profile = samply.load(recording)
        c = samply.callers(
            profile,
            _resolve(profile, symbol),
            _thread(thread),
            depth=depth,
            n=n,
            wall=wall,
            merge_tiers=merge_tiers,
            time=_resolve_time_arg(profile, time),
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
        merge_tiers: Annotated[
            bool,
            Field(description=MERGE_TIERS_ARG),
        ] = False,
        time: Annotated[str | None, Field(description=TIME_ARG)] = None,
    ) -> CallToolResult:
        """Collapsed stacks: every hot root-to-leaf path with its share of the
        thread's time, heaviest first ("a;b;c <ms>").

        With focus, paths start at that function, which shows where its
        inclusive time goes.
        """
        profile = samply.load(recording)
        if focus is not None:
            focus = _resolve(profile, focus)
        fg = samply.flamegraph(
            profile,
            _thread(thread),
            focus=focus,
            depth=depth,
            min_pct=min_pct,
            wall=wall,
            native_root=native_root,
            merge_tiers=merge_tiers,
            time=_resolve_time_arg(profile, time),
        )
        return _text_result(render.format_flamegraph(fg))

    @mcp.tool()
    def samply_diff(
        baseline: Annotated[str, Field(description="baseline samply recording")],
        experiment: Annotated[str, Field(description="experiment samply recording")],
        thread: Annotated[
            str | None,
            Field(
                description=(
                    "thread name, looked up in both recordings (default: each"
                    " one's thread with the most CPU time)"
                )
            ),
        ] = None,
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
        baseline_time: Annotated[str | None, Field(description=TIME_ARG)] = None,
        experiment_time: Annotated[str | None, Field(description=TIME_ARG)] = None,
    ) -> CallToolResult:
        """Compare two samply recordings (or two time slices of the same
        recording via baseline_time / experiment_time): per-function self time
        as a share of each slice's thread time, biggest change first. "new" and
        "gone" mark functions present in only one side."""
        base = samply.load(baseline)
        exp = samply.load(experiment)
        rows = samply.diff(
            base,
            exp,
            _thread(thread),
            n=n,
            wall=wall,
            merge_tiers=merge_tiers,
            baseline_time=_resolve_time_arg(base, baseline_time),
            experiment_time=_resolve_time_arg(exp, experiment_time),
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
        time: Annotated[str | None, Field(description=TIME_ARG)] = None,
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
        profile = samply.load(recording)
        a = samply.annotate(
            profile,
            _resolve(profile, symbol),
            _thread(thread),
            code=code,
            wall=wall,
            time=_resolve_time_arg(profile, time),
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
        time: Annotated[str | None, Field(description=TIME_ARG)] = None,
    ) -> CallToolResult:
        """A window of the samply_annotate listing around a line, for reading
        beyond the hot blocks."""
        profile = samply.load(recording)
        a = samply.annotate(
            profile,
            _resolve(profile, symbol),
            _thread(thread),
            code=code,
            wall=wall,
            time=_resolve_time_arg(profile, time),
        )
        return _text_result(render.format_read_around(a, line, context=context))

    @mcp.tool()
    def samply_slices(
        recording: Annotated[str, Field(description=RECORDING_ARG)],
        thread: Annotated[str | None, Field(description=THREAD_ARG)] = None,
        buckets: Annotated[
            int,
            Field(
                description=(
                    "number of equal-time buckets when the recording has no"
                    " iteration markers or timings.tsv sidecar"
                )
            ),
        ] = 5,
        min_iteration: Annotated[
            int,
            Field(
                description=(
                    "lowest iteration index included in worst/best/average"
                    " buckets (default 1, excluding First=0)"
                )
            ),
        ] = 1,
        wall: Annotated[bool, Field(description=WALL_ARG)] = False,
    ) -> CallToolResult:
        """Inspect time slices of a samply recording: JS3 iteration buckets
        (first, worst, average, best, iters) when iteration markers or a
        timings.tsv sidecar exist, otherwise equal-time buckets across the
        thread's span. Use the reported slice names or time args with `time=`
        on samply_hotspots/callers/flamegraph/annotate or `baseline_time=` /
        `experiment_time=` on samply_diff."""
        profile = samply.load(recording)
        sl = samply.slices(
            profile,
            _thread(thread),
            buckets=buckets,
            min_iteration=min_iteration,
            wall=wall,
        )
        _slice_cache[_symbol_key(profile)] = {
            s.name.lower(): s.time_arg for s in sl.slices
        }
        return _text_result(render.format_slices(profile, sl))

    @mcp.tool()
    def samply_threads(
        recording: Annotated[str, Field(description=RECORDING_ARG)],
        top: Annotated[int, Field(description="hottest functions per thread")] = 3,
    ) -> CallToolResult:
        """Every thread in a samply recording with wall time, CPU time and
        CPU/wall ratio (low means blocked or idle), busiest first, plus its
        hottest functions. Use the tid as `thread` in the other samply_*
        tools; d8's background compiler and GC threads share one name."""
        profile = samply.load(recording)
        return _text_result(
            render.format_threads(samply.threads(profile, top=top), profile)
        )
