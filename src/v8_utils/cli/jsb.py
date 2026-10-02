"""jsb - JetStream bench runner for d8.

Run a specific JetStream2/3 story with one or more d8 builds, with support
for multi-run aggregation, build/flag comparison, and debugger/profiler modes.

Usage:
  jsb BENCH [-b BUILD[:FLAGS]]... [-n RUNS] [--js2] [--gdb|--rr|--perf|--perf-upload]

Build spec syntax:
  release-main            # no extra flags
  release-lto:--turbolev  # with extra d8/JS flags after the colon
  /path/to/d8             # full path to d8 binary
  /path/to/d8:--turbolev  # full path with extra flags
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from rich.console import Console

from ..api import config as cfg_module
from ..api.jsb import Variant, compare, run_perf, run_round_robin, run_v8log
from ..render.jsb import format_comparison


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    # `jsb config` is handled before the bench parser so that "config" is
    # never mistaken for a benchmark name.
    if argv and argv[0] == "config":
        print(cfg_module.template())
        return

    p = argparse.ArgumentParser(
        prog="jsb",
        description="JetStream bench runner for d8",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
subcommands:
  config                                               # print config template

examples:
  jsb regexp-octane                                    # single run, passthrough
  jsb regexp-octane -b release -n 5                   # 5 runs, aggregated
  jsb regexp-octane -b release-main -b release-lto -n 4  # compare two builds
  jsb regexp-octane -b release -b 'release:--turbolev' -n 4  # compare flags
  jsb crypto-md5-SP -b release --js2                  # JetStream2
  jsb crypto-md5-SP -b release --gdb                  # run under gdb
  jsb crypto-md5-SP -b release --rr                   # record with rr
  jsb crypto-md5-SP -b release --perf                 # linux-perf-d8.py
  jsb crypto-md5-SP -b release --v8log                # record v8.log
  jsb regexp-octane -b ~/v8-alt/out/release/d8        # full d8 path
""",
    )
    from ..api.update import current_version

    p.add_argument(
        "--version", action="version", version=f"%(prog)s {current_version()}"
    )
    p.add_argument(
        "lineitems",
        nargs="*",
        help="Benchmark story names, e.g. regexp-octane chai-wtb (omit to run full suite)",
    )
    p.add_argument(
        "-b",
        "--build",
        dest="builds",
        action="append",
        default=[],
        metavar="BUILD_OR_PATH[:FLAGS]",
        help="Build name under v8_out (or full path to d8), optionally "
        "with d8 flags after ':'. Repeatable — each -b creates one variant.",
    )
    p.add_argument(
        "-n",
        "--runs",
        type=int,
        default=1,
        help="Number of runs per variant (default: 1)",
    )
    p.add_argument(
        "--show-all",
        action="store_true",
        help="Show all metrics (default: hide non-significant when comparing)",
    )
    p.add_argument(
        "--js2", action="store_true", help="Use JetStream2 (default: JetStream3)"
    )
    p.add_argument(
        "--gdb", action="store_true", help="Run under gdb (single variant, single run)"
    )
    p.add_argument(
        "--rr",
        action="store_true",
        help="Run under rr record (single variant, single run)",
    )
    perf_group = p.add_mutually_exclusive_group()
    perf_group.add_argument(
        "--perf",
        action="store_true",
        help="Record a perf trace locally via linux-perf-d8.py (single variant)",
    )
    perf_group.add_argument(
        "--perf-upload",
        action="store_true",
        help="Record a perf trace and upload via pprof (single variant)",
    )
    perf_group.add_argument(
        "--v8log",
        action="store_true",
        help="Record a v8.log profiling trace (single variant)",
    )
    args = p.parse_args(argv or None)
    lineitems = args.lineitems or None  # empty list → None (full suite)

    cfg = cfg_module.load()
    v8_out = cfg.v8_out
    suite_dir = cfg.repos["js2"].path if args.js2 else cfg.repos["js3"].path
    suite = "JS2" if args.js2 else "JS3"
    js3 = not args.js2

    builds = args.builds or [cfg.default_build]
    variants = [Variant.parse(b) for b in builds]

    for v in variants:
        d8 = v.d8(v8_out)
        if not d8.exists():
            sys.exit(f"error: d8 not found: {d8}")

    # --- v8.log recording ---
    if args.v8log:
        if len(variants) != 1:
            sys.exit("error: --v8log requires exactly one build")
        v = variants[0]
        try:
            log_path = run_v8log(v, suite_dir, lineitems, v8_out)
        except RuntimeError as e:
            sys.exit(f"error: {e}")
        print(log_path)
        return

    # --- Profiling ---
    if args.perf or args.perf_upload:
        if len(variants) != 1:
            sys.exit("error: --perf/--perf-upload requires exactly one build")
        v = variants[0]
        result = run_perf(
            v,
            suite_dir,
            lineitems,
            v8_out,
            cfg.perf_script,
            upload=args.perf_upload,
        )
        print(result)
        return

    # --- Debugger (single variant, single run, passthrough) ---
    if args.gdb or args.rr:
        if len(variants) != 1:
            sys.exit("error: --gdb/--rr requires exactly one build")
        v = variants[0]
        cmd = v.cmd(v.d8(v8_out), suite_dir, lineitems)
        cmd = (["gdb", "--args"] if args.gdb else ["rr", "record"]) + cmd
        subprocess.run(cmd, cwd=suite_dir)
        return

    # --- Single variant, single run: pure passthrough ---
    if args.runs == 1 and len(variants) == 1:
        v = variants[0]
        subprocess.run(v.cmd(v.d8(v8_out), suite_dir, lineitems), cwd=suite_dir)
        return

    # --- Multi-run / multi-variant: capture, parse, print table ---
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    total_runs = len(variants) * args.runs
    progress = (
        Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(bar_width=20),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=Console(stderr=True),
            transient=True,
        )
        if sys.stderr.isatty()
        else None
    )
    task = progress.add_task("running", total=total_runs) if progress else None

    def on_run(round_i: int, vi: int) -> None:
        if progress and task is not None:
            progress.advance(task)

    try:
        if progress:
            progress.start()
        results = run_round_robin(
            variants,
            suite_dir,
            lineitems,
            args.runs,
            js3,
            v8_out,
            on_run=on_run if progress else None,
        )
    except RuntimeError as e:
        if progress:
            progress.stop()
        sys.exit(f"error: {e}")
    if progress:
        progress.stop()
    print(
        format_comparison(
            compare(lineitems, suite, args.runs, variants, results),
            show_all=args.show_all,
            ansi=sys.stderr.isatty(),
        )
    )


if __name__ == "__main__":
    main()
