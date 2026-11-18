"""MCP tools for V8 performance investigation: d8, jsb, v8log, godbolt, llvm-mca."""

from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from ..api import d8, godbolt, jsb, mca, trace_index, v8log
from ..render.jsb import format_comparison
from ._shared import _text_result

SUMMARY = (
    "run_d8 (execute JS in V8 shell), "
    "godbolt_* (Compiler Explorer), "
    "llvm_mca (assembly throughput), "
    "d8_trace_index (V8 traces), "
    "v8log_analyze (V8 logs: deopts, "
    "ICs, "
    "maps, "
    "profile), "
    "jsb_run_bench (run/compare JS benchmarks)"
)


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
        return _text_result(
            d8.run_d8(
                args=args,
                d8_path=d8_path,
                cwd=cwd,
                timeout=timeout,
                output_file=output_file,
            )
        )

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
            list[str] | None,
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
        ] = None,
        runs: Annotated[int, Field(description="number of runs per variant")] = 5,
        suite: Annotated[str, Field(description='"js2" or "js3"')] = "js3",
        record: Annotated[
            str | None,
            Field(
                description=(
                    "profiling mode -- omit to run for scores (the default)."
                    ' Options: "perf" records with the configured profiler:'
                    " linux-perf returns the perf.data path for"
                    " perf_hotspots/perf_annotate, samply (the macOS default)"
                    " returns a recording directory for samply_hotspots and the"
                    " other samply_* tools;"
                    ' "perf_upload" records with linux-perf and uploads via pprof;'
                    ' "v8log" records a v8.log and returns its path for'
                    " v8log_analyze. Every record mode requires exactly one"
                    " binary."
                )
            ),
        ] = None,
        sample_rate: Annotated[
            int | None,
            Field(
                description=(
                    'sampling rate in Hz for record="perf" with samply (default:'
                    " the configured sample_rate, 1000). Short stories need a"
                    " higher rate, e.g. 10000, for enough samples to annotate."
                )
            ),
        ] = None,
    ) -> CallToolResult:
        """Run a JetStream2/3 story with one or more JS shell binaries and return scores.

        Returns a comparison table with mean, stdev, delta, p-value
        (Welch's t-test), and confidence (high/medium/low) per metric.
        """
        if binaries is None:
            binaries = []
        if record is not None:
            return _text_result(
                jsb.jsb_record(lineitems, binaries, suite, record, sample_rate)
            )
        if sample_rate is not None:
            raise ValueError('sample_rate needs record="perf"')
        return _text_result(
            format_comparison(jsb.jsb_compare(lineitems, binaries, runs, suite))
        )

    @mcp.tool()
    def d8_trace_index(
        path: Annotated[str, Field(description="path to the trace file")],
    ) -> CallToolResult:
        """Build a table of contents for a V8 trace file.

        Recognizes sections from --trace-turbo-graph, --print-maglev-graphs,
        --trace-maglev-graph-building, --trace-opt, --trace-deopt, and
        --print-code. Use the line numbers to navigate with read_around.
        """
        return _text_result(trace_index.d8_trace_index(path=path))

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
        return _text_result(
            mca.llvm_mca(
                assembly=assembly,
                arch=arch,
                cpu=cpu,
                syntax=syntax,
                bottleneck=bottleneck,
                timeline=timeline,
            )
        )

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
        return _text_result(
            godbolt.godbolt_compile(
                source=source,
                arch=arch,
                compiler=compiler,
                language=language,
                flags=flags,
                mca=mca,
                opt_remarks=opt_remarks,
            )
        )

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
        return _text_result(
            godbolt.godbolt_list_compilers(language=language, filter=filter)
        )

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
