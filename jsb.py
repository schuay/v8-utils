"""jsb - JetStream bench runner for d8.

Run a specific JetStream2/3 story with one or more d8 builds, with support
for multi-run aggregation, build/flag comparison, and debugger/profiler modes.

Usage:
  jsb BENCH [-b BUILD[:FLAGS]]... [-n RUNS] [--js2] [--gdb|--rr|--perf|--perf-upload]

Build spec syntax:
  release-main            # no extra flags
  release-lto:--turbolev  # with extra d8/JS flags after the colon
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, stdev

from scipy.stats import ttest_ind

import config as cfg_module


# ---------- Variant ----------


@dataclass
class Variant:
    build: str
    flags: str = ""

    @classmethod
    def parse(cls, spec: str) -> Variant:
        """Parse 'build[:flags]' spec."""
        if ":" in spec:
            build, flags = spec.split(":", 1)
            return cls(build=build.strip(), flags=flags.strip())
        return cls(build=spec.strip())

    @property
    def label(self) -> str:
        return f"{self.build} [{self.flags}]" if self.flags else self.build

    def d8(self, v8_out: Path) -> Path:
        return v8_out / self.build / "d8"

    def cmd(self, d8: Path, suite_dir: Path, bench: str) -> list[str]:
        flags = self.flags.split() if self.flags else []
        return [str(d8)] + flags + [str(suite_dir / "cli.js"), "--", bench]


# ---------- Output parsing ----------

# JS2: "crypto-md5-SP Startup-Score: 195.787"
_JS2_SCORE = re.compile(r"^\S+\s+([\w-]+-Score):\s+([\d.]+)\s*$")

# JS3: "chai-wtb First-Score    61.50 pts"
# JS3: "chai-wtb Score          97.20 pts"
_JS3_SCORE = re.compile(r"^\S+\s+([\w-]*Score)\s+([\d.]+)\s+pts\s*$")


def parse_js2(output: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for line in output.splitlines():
        if m := _JS2_SCORE.match(line):
            scores[m.group(1)] = float(m.group(2))
    return scores


def parse_js3(output: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for line in output.splitlines():
        # Skip "Overall *" lines — they duplicate per-bench scores
        if line.startswith("Overall"):
            continue
        if m := _JS3_SCORE.match(line):
            scores[m.group(1)] = float(m.group(2))
    return scores


# ---------- Running ----------


def _run_captured(cmd: list[str], cwd: Path, js3: bool) -> dict[str, float]:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"d8 exited with code {result.returncode}\n{output}")
    out = result.stdout + result.stderr
    return parse_js3(out) if js3 else parse_js2(out)


def run_variant(
    variant: Variant,
    suite_dir: Path,
    bench: str,
    n: int,
    js3: bool,
    v8_out: Path,
    progress: bool = False,
) -> dict[str, list[float]]:
    """Run one variant N times. Returns metric → list of values."""
    d8 = variant.d8(v8_out)
    cmd = variant.cmd(d8, suite_dir, bench)
    all_scores: dict[str, list[float]] = {}
    if progress:
        print(f"{variant.label}: ", end="", flush=True, file=sys.stderr)
    for _ in range(n):
        scores = _run_captured(cmd, suite_dir, js3)
        for metric, val in scores.items():
            all_scores.setdefault(metric, []).append(val)
        if progress:
            print(".", end="", flush=True, file=sys.stderr)
    if progress:
        print(file=sys.stderr)
    return all_scores


def run_perf(
    variant: Variant,
    suite_dir: Path,
    bench: str,
    v8_out: Path,
    perf_script: Path,
    upload: bool = False,
) -> str:
    """Record a perf trace via linux-perf-d8.py.

    Returns the output from linux-perf-d8.py (includes the perf.data path).
    When upload=False, passes --skip-pprof to keep the trace local.
    """
    extra = [] if upload else ["--skip-pprof"]
    cmd = (
        ["python3", str(perf_script)]
        + extra
        + [str(variant.d8(v8_out))]
        + (variant.flags.split() if variant.flags else [])
        + [str(suite_dir / "cli.js"), "--", bench]
    )
    r = subprocess.run(cmd, cwd=suite_dir, capture_output=True, text=True)
    output = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        raise RuntimeError(
            f"linux-perf-d8.py failed (exit {r.returncode}):\n{output[:1000]}"
        )
    return output


# ---------- Formatting ----------

_METRIC_ORDER = [
    "Score",
    "Total-Score",
    "First-Score",
    "Startup-Score",
    "Worst-Score",
    "Worst-Case-Score",
    "Average-Score",
]


def _fmt_stat(vals: list[float]) -> str:
    if len(vals) == 1:
        return f"{vals[0]:.2f}"
    m = mean(vals)
    s = stdev(vals)
    pct = 100 * s / m if m else 0.0
    return f"{m:.2f} ±{pct:.1f}%"


def _fmt_delta(base: list[float], exp: list[float]) -> tuple[str, str]:
    """Return (delta_str, significance_marker). Welch's t-test, α=0.05."""
    bm, em = mean(base), mean(exp)
    if bm == 0:
        return "N/A", ""
    d = 100 * (em - bm) / bm
    delta = f"{'+' if d > 0 else ''}{d:.1f}%"
    if len(base) >= 2 and len(exp) >= 2:
        _, p = ttest_ind(base, exp, equal_var=False)
        marker = "*" if p < 0.05 else ""
    else:
        marker = ""
    return delta, marker


def format_table(
    bench: str,
    suite: str,
    n: int,
    variants: list[Variant],
    results: list[dict[str, list[float]]],
) -> str:
    all_metrics: set[str] = set()
    for r in results:
        all_metrics.update(r.keys())
    ordered = [m for m in _METRIC_ORDER if m in all_metrics]
    ordered += sorted(all_metrics - set(ordered))

    labels = [v.label for v in variants]
    mcol = max(16, max((len(m) for m in ordered), default=10) + 2)
    vcol = max(18, max(len(l) for l in labels) + 2)
    has_delta = len(variants) == 2
    dcol = 12  # wide enough for "+18.0%  *"

    lines = [f"\n{bench}  ({suite}, {n} run{'s' if n > 1 else ''})\n"]

    hdr = f"{'Metric':<{mcol}}" + "".join(f"{l:>{vcol}}" for l in labels)
    if has_delta:
        hdr += f"{'delta':>{dcol}}"
    lines += [hdr, "-" * len(hdr)]

    any_significant = False
    for metric in ordered:
        row = f"{metric:<{mcol}}"
        vals_list = [r.get(metric, []) for r in results]
        for vals in vals_list:
            row += f"{_fmt_stat(vals) if vals else 'N/A':>{vcol}}"
        if has_delta and vals_list[0] and vals_list[1]:
            delta, marker = _fmt_delta(vals_list[0], vals_list[1])
            if marker:
                any_significant = True
            row += f"{delta + ' ' + marker:>{dcol}}"
        lines.append(row)

    if has_delta and n >= 2:
        lines.append("")
        lines.append(
            "* p < 0.05 (Welch's t-test)"
            if any_significant
            else "(no statistically significant differences)"
        )

    return "\n".join(lines)


# ---------- Stats helper (used by MCP tool) ----------


def summarise(results: list[dict[str, list[float]]]) -> list[dict]:
    """Convert raw run lists to per-variant summary dicts for MCP output."""
    out = []
    for r in results:
        variant_summary: dict[str, dict] = {}
        for metric, vals in r.items():
            m = mean(vals)
            s = stdev(vals) if len(vals) > 1 else 0.0
            variant_summary[metric] = {
                "values": vals,
                "mean": round(m, 3),
                "stdev": round(s, 3),
                "stdev_pct": round(100 * s / m, 2) if m else 0.0,
            }
        out.append(variant_summary)

    # Attach p-values when there are exactly two variants
    if len(results) == 2:
        a, b = results
        for metric in a.keys() & b.keys():
            va, vb = a[metric], b[metric]
            if len(va) >= 2 and len(vb) >= 2:
                _, p = ttest_ind(va, vb, equal_var=False)
                out[0][metric]["p_value"] = round(float(p), 4)
                out[1][metric]["p_value"] = round(float(p), 4)

    return out


# ---------- CLI ----------


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
""",
    )
    p.add_argument("bench", help="Benchmark story name, e.g. regexp-octane")
    p.add_argument(
        "-b",
        "--build",
        dest="builds",
        action="append",
        default=[],
        metavar="BUILD[:FLAGS]",
        help="Build name under v8_out, optionally with d8 flags after ':'. "
        "Repeatable — each -b creates one variant.",
    )
    p.add_argument(
        "-n",
        "--runs",
        type=int,
        default=1,
        help="Number of runs per variant (default: 1)",
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
    args = p.parse_args(argv or None)

    cfg = cfg_module.load()
    v8_out = cfg.v8_out
    suite_dir = cfg.js2_dir if args.js2 else cfg.js3_dir
    suite = "JS2" if args.js2 else "JS3"
    js3 = not args.js2

    builds = args.builds or [cfg.default_build]
    variants = [Variant.parse(b) for b in builds]

    for v in variants:
        d8 = v.d8(v8_out)
        if not d8.exists():
            sys.exit(f"error: d8 not found: {d8}")

    # --- Profiling ---
    if args.perf or args.perf_upload:
        if len(variants) != 1:
            sys.exit("error: --perf/--perf-upload requires exactly one build")
        v = variants[0]
        result = run_perf(
            v, suite_dir, args.bench, v8_out, cfg.perf_script, upload=args.perf_upload
        )
        print(result)
        return

    # --- Debugger (single variant, single run, passthrough) ---
    if args.gdb or args.rr:
        if len(variants) != 1:
            sys.exit("error: --gdb/--rr requires exactly one build")
        v = variants[0]
        cmd = v.cmd(v.d8(v8_out), suite_dir, args.bench)
        cmd = (["gdb", "--args"] if args.gdb else ["rr", "record"]) + cmd
        subprocess.run(cmd, cwd=suite_dir)
        return

    # --- Single variant, single run: pure passthrough ---
    if args.runs == 1 and len(variants) == 1:
        v = variants[0]
        subprocess.run(v.cmd(v.d8(v8_out), suite_dir, args.bench), cwd=suite_dir)
        return

    # --- Multi-run / multi-variant: capture, parse, print table ---
    try:
        results = [
            run_variant(v, suite_dir, args.bench, args.runs, js3, v8_out, progress=True)
            for v in variants
        ]
    except RuntimeError as e:
        print(file=sys.stderr)  # end progress line if interrupted mid-run
        sys.exit(f"error: {e}")
    print(format_table(args.bench, suite, args.runs, variants, results))


if __name__ == "__main__":
    main()
