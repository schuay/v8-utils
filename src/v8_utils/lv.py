"""lv — V8 log viewer CLI.

Usage:
  lv deopts <logfile> [--top N] [--filter GLOB]
  lv ics <logfile> [--top N] [--filter GLOB]
  lv maps <logfile> [--top N] [--verbose]
  lv fn <logfile> <pattern>
  lv profile <logfile> [--top N] [--filter GLOB]
  lv vms <logfile>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import v8log

# ── ANSI colors (no-ops when not a TTY) ──────────────────────────────────────

if sys.stdout.isatty():
    _RED = "\033[31m"
    _RESET = "\033[0m"
else:
    _RED = _RESET = ""


# ── Progress bar ─────────────────────────────────────────────────────────────


def _make_progress_cb():
    """Return (context_manager, on_progress_callback) or (None, None)."""
    if not sys.stderr.isatty():
        return None, None
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(bar_width=20),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=Console(stderr=True),
        transient=True,
    )
    task_id = None

    def on_progress(done: int, total: int) -> None:
        nonlocal task_id
        if task_id is None:
            task_id = progress.add_task("Parsing", total=total)
        progress.update(task_id, completed=done, total=total)

    return progress, on_progress


def _parse_log(path: Path) -> v8log.V8Log:
    progress, on_progress = _make_progress_cb()
    if progress:
        with progress:
            return v8log.V8Log.parse(path, on_progress=on_progress)
    return v8log.V8Log.parse(path)


# ── Subcommand handlers ─────────────────────────────────────────────────────


def _cmd_deopts(args: argparse.Namespace) -> None:
    log = _parse_log(args.logfile)
    summary = v8log.analyze_deopts(log, top=args.top, filter_pat=args.filter)
    print(v8log.format_deopts(summary, ansi=sys.stdout.isatty()))


def _cmd_ics(args: argparse.Namespace) -> None:
    log = _parse_log(args.logfile)
    summary = v8log.analyze_ics(log, top=args.top, filter_pat=args.filter)
    print(v8log.format_ics(summary, ansi=sys.stdout.isatty()))


def _cmd_maps(args: argparse.Namespace) -> None:
    log = _parse_log(args.logfile)
    summary = v8log.analyze_maps(log, top=args.top)
    print(v8log.format_maps(summary, ansi=sys.stdout.isatty(), verbose=args.verbose))


def _cmd_fn(args: argparse.Namespace) -> None:
    log = _parse_log(args.logfile)
    summary = v8log.analyze_fn(log, pattern=args.pattern)
    print(v8log.format_fn(summary, ansi=sys.stdout.isatty()))


def _cmd_profile(args: argparse.Namespace) -> None:
    log = _parse_log(args.logfile)
    summary = v8log.analyze_profile(log, top=args.top, filter_pat=args.filter)
    print(v8log.format_profile(summary, ansi=sys.stdout.isatty()))


def _cmd_vms(args: argparse.Namespace) -> None:
    log = _parse_log(args.logfile)
    summary = v8log.analyze_vms(log)
    print(v8log.format_vms(summary, ansi=sys.stdout.isatty()))


# ── CLI entry point ──────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(prog="lv", description="V8 log viewer")
    sub = parser.add_subparsers(dest="command", required=True)

    # deopts
    p = sub.add_parser("deopts", help="Deoptimization summary")
    p.add_argument("logfile", type=Path, help="Path to v8.log")
    p.add_argument("--top", type=int, default=20, help="Max rows (default: 20)")
    p.add_argument("--filter", type=str, help="Filter by function name glob")
    p.set_defaults(func=_cmd_deopts)

    # ics
    p = sub.add_parser("ics", help="Inline cache summary")
    p.add_argument("logfile", type=Path, help="Path to v8.log")
    p.add_argument("--top", type=int, default=20, help="Max rows (default: 20)")
    p.add_argument("--filter", type=str, help="Filter by function name glob")
    p.set_defaults(func=_cmd_ics)

    # maps
    p = sub.add_parser("maps", help="Map transition summary")
    p.add_argument("logfile", type=Path, help="Path to v8.log")
    p.add_argument("--top", type=int, default=20, help="Max rows (default: 20)")
    p.add_argument("--verbose", action="store_true", help="Show full map-details")
    p.set_defaults(func=_cmd_maps)

    # fn
    p = sub.add_parser("fn", help="Function-centric view")
    p.add_argument("logfile", type=Path, help="Path to v8.log")
    p.add_argument("pattern", type=str, help="Function name glob pattern")
    p.set_defaults(func=_cmd_fn)

    # profile
    p = sub.add_parser("profile", help="Tick profile (flat)")
    p.add_argument("logfile", type=Path, help="Path to v8.log")
    p.add_argument("--top", type=int, default=20, help="Max rows (default: 20)")
    p.add_argument("--filter", type=str, help="Filter by function name glob")
    p.set_defaults(func=_cmd_profile)

    # vms
    p = sub.add_parser("vms", help="VM state breakdown")
    p.add_argument("logfile", type=Path, help="Path to v8.log")
    p.set_defaults(func=_cmd_vms)

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f"{_RED}error:{_RESET} {e}", file=sys.stderr)
        sys.exit(1)
