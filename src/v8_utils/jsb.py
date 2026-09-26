"""jsb - JetStream bench runner for d8: variants, runs, and result tables.

The command-line frontend is v8_utils.cli.jsb.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, stdev

from rich import box
from rich.console import Console
from rich.table import Table
from scipy.stats import ttest_ind


# ---------- Variant ----------


@dataclass
class Variant:
    build: str
    flags: str = ""
    _d8_path: Path | None = None

    @classmethod
    def parse(cls, spec: str) -> Variant:
        """Parse 'build[:flags]' or '/path/to/d8[:flags]' spec.

        When the build part contains a '/' it is treated as a direct path
        to a d8 binary, bypassing v8_out resolution.
        """
        if ":" in spec:
            build, flags = spec.split(":", 1)
            build, flags = build.strip(), flags.strip()
        else:
            build, flags = spec.strip(), ""

        if "/" in build:
            d8_path = Path(build).expanduser()
            # Use parent dir as label (e.g. ".../release/d8" → "release")
            label = d8_path.parent.name if d8_path.name == "d8" else d8_path.name
            return cls(build=label, flags=flags, _d8_path=d8_path)
        return cls(build=build, flags=flags)

    @property
    def label(self) -> str:
        return f"{self.build} [{self.flags}]" if self.flags else self.build

    def d8(self, v8_out: Path) -> Path:
        if self._d8_path is not None:
            return self._d8_path
        return v8_out / self.build / "d8"

    def cmd(
        self, d8: Path, suite_dir: Path, lineitems: list[str] | None = None
    ) -> list[str]:
        flags = self.flags.split() if self.flags else []
        cmd = [str(d8)] + flags + [str(suite_dir / "cli.js")]
        if lineitems:
            cmd += ["--", ",".join(lineitems)]
        return cmd


# ---------- Output parsing ----------

# JS2: "crypto-md5-SP Startup-Score: 195.787"
_JS2_SCORE = re.compile(r"^\S+\s+([\w-]+-Score):\s+([\d.]+)\s*$")

# JS2 suite total, indented under "Totals:": "    Total-Score: 353.812".
# The "Mean-Scores:" block is deliberately ignored: those per-category geomeans
# aggregate subscores that only some benchmarks report.
_JS2_TOTAL = re.compile(r"^\s+Total-Score:\s+([\d.]+)\s*$")

# JS3: "chai-wtb First-Score    61.50 pts"
# JS3: "chai-wtb Score          97.20 pts"
_JS3_SCORE = re.compile(r"^\S+\s+([\w-]*Score)\s+([\d.]+)\s+pts\s*$")

# Metric key for the suite-wide geomean over per-benchmark Scores.
OVERALL = "Overall/Score"


def parse_js2(output: str, full_names: bool = False) -> dict[str, float]:
    scores: dict[str, float] = {}
    for line in output.splitlines():
        if m := _JS2_SCORE.match(line):
            key = f"{line.split()[0]}/{m.group(1)}" if full_names else m.group(1)
            scores[key] = float(m.group(2))
        # The indented total only differs from the single benchmark's own score
        # once more than one benchmark ran.
        elif full_names and (m := _JS2_TOTAL.match(line)):
            scores[OVERALL] = float(m.group(1))
    return scores


def parse_js3(output: str, full_names: bool = False) -> dict[str, float]:
    scores: dict[str, float] = {}
    for line in output.splitlines():
        if line.startswith("Overall"):
            # Keep only the headline suite Score. The per-category means
            # aggregate subscores that only some benchmarks report, and with a
            # single benchmark the whole block just duplicates its own scores.
            if full_names and (m := _JS3_SCORE.match(line)) and m.group(1) == "Score":
                scores[OVERALL] = float(m.group(2))
            continue
        if m := _JS3_SCORE.match(line):
            key = f"{line.split()[0]}/{m.group(1)}" if full_names else m.group(1)
            scores[key] = float(m.group(2))
    return scores


# ---------- Running ----------


def _run_captured(
    cmd: list[str], cwd: Path, js3: bool, full_names: bool = False
) -> dict[str, float]:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"d8 exited with code {result.returncode}\n{output}")
    out = result.stdout + result.stderr
    return parse_js3(out, full_names) if js3 else parse_js2(out, full_names)


def run_round_robin(
    variants: list[Variant],
    suite_dir: Path,
    lineitems: list[str] | None,
    n: int,
    js3: bool,
    v8_out: Path,
    on_run: Callable[[int, int], None] | None = None,
) -> list[dict[str, list[float]]]:
    """Run all variants interleaved in round-robin order, N rounds total.

    Returns one result dict (metric → list of values) per variant.
    on_run(round_i, variant_i): called after each completed run.
    """
    full_names = lineitems is None or len(lineitems) > 1
    cmds = [v.cmd(v.d8(v8_out), suite_dir, lineitems) for v in variants]
    all_scores: list[dict[str, list[float]]] = [{} for _ in variants]
    for round_i in range(n):
        for vi, cmd in enumerate(cmds):
            scores = _run_captured(cmd, suite_dir, js3, full_names=full_names)
            for metric, val in scores.items():
                all_scores[vi].setdefault(metric, []).append(val)
            if on_run:
                on_run(round_i, vi)
    return all_scores


def run_v8log(
    variant: Variant,
    suite_dir: Path,
    lineitems: list[str] | None,
    v8_out: Path,
    output: Path | None = None,
) -> Path:
    """Record a v8.log profiling trace.

    Runs d8 with --prof --log-ic --log-maps --log-deopt and returns the
    path to the generated log file.
    """
    log_path = output or (suite_dir / "v8.log")
    extra_flags = [
        "--log-all",
        f"--logfile={log_path}",
    ]
    d8 = variant.d8(v8_out)
    flags = (variant.flags.split() if variant.flags else []) + extra_flags
    cmd = [str(d8)] + flags + [str(suite_dir / "cli.js")]
    if lineitems:
        cmd += ["--", ",".join(lineitems)]
    r = subprocess.run(cmd, cwd=suite_dir, capture_output=True, text=True)
    if r.returncode != 0:
        output_text = (r.stdout + r.stderr).strip()
        raise RuntimeError(f"d8 exited with code {r.returncode}\n{output_text[:1000]}")
    if not log_path.exists():
        raise RuntimeError(f"v8.log not found at {log_path} after run")
    return log_path


def run_perf(
    variant: Variant,
    suite_dir: Path,
    lineitems: list[str] | None,
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
        + [str(suite_dir / "cli.js")]
    )
    if lineitems:
        cmd += ["--", ",".join(lineitems)]
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
_METRIC_RANK = {name: i for i, name in enumerate(_METRIC_ORDER)}


def _metric_sort_key(metric: str) -> tuple[int, str, int, str]:
    """Order rows: suite aggregates first, then grouped by benchmark, with the
    headline Score leading each group. Applies to both "bench/sub" keys and the
    bare sub-score keys used for a single benchmark."""
    bench, _, sub = metric.rpartition("/")
    rank = _METRIC_RANK.get(sub, len(_METRIC_ORDER))
    return (0 if bench == "Overall" else 1, bench, rank, sub)


def _fmt_stat(vals: list[float]) -> str:
    if len(vals) == 1:
        return f"{vals[0]:.2f}"
    m = mean(vals)
    s = stdev(vals)
    pct = 100 * s / m if m else 0.0
    return f"{m:.2f} ±{pct:.1f}%"


def _p_confidence(p: float) -> str:
    """Map a p-value to a human-readable confidence level."""
    if p < 0.01:
        return "high"
    if p < 0.05:
        return "medium"
    return "low"


def _fmt_delta(base: list[float], exp: list[float]) -> tuple[str, float | None, str]:
    """Return (delta_str, p_value, confidence). Welch's t-test."""
    bm, em = mean(base), mean(exp)
    if bm == 0:
        return "N/A", None, ""
    d = 100 * (em - bm) / bm
    delta = f"{'+' if d > 0 else ''}{d:.1f}%"
    if len(base) >= 2 and len(exp) >= 2:
        _, p = ttest_ind(base, exp, equal_var=False)
        return delta, float(p), _p_confidence(p)
    return delta, None, ""


def format_table(
    lineitems: list[str] | None,
    suite: str,
    n: int,
    variants: list[Variant],
    results: list[dict[str, list[float]]],
    show_all: bool = False,
    ansi: bool = False,
) -> str:
    all_metrics: set[str] = set()
    for r in results:
        all_metrics.update(r.keys())
    ordered = sorted(all_metrics, key=_metric_sort_key)

    has_comparison = len(variants) >= 2

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold", padding=(0, 1))
    table.add_column("metric")
    table.add_column(variants[0].label, justify="right")
    for v in variants[1:]:
        table.add_column(v.label, justify="right")
        table.add_column("chg%", justify="right")
        table.add_column("p", justify="right")
        table.add_column("confidence", justify="right")

    omitted = 0
    for metric in ordered:
        base_vals = results[0].get(metric, [])
        cells: list[str] = [metric, _fmt_stat(base_vals) if base_vals else "N/A"]
        any_sig = False
        for i in range(1, len(variants)):
            exp_vals = results[i].get(metric, [])
            cells.append(_fmt_stat(exp_vals) if exp_vals else "N/A")
            if base_vals and exp_vals:
                delta, p, conf = _fmt_delta(base_vals, exp_vals)
                # JetStream: bigger is always better
                if delta.startswith("+"):
                    style = "green"
                elif delta.startswith("-"):
                    style = "red"
                else:
                    style = ""
                cells.append(f"[{style}]{delta}[/]" if style else delta)
                cells.append(f"{p:.4f}" if p is not None else "")
                cells.append(conf)
                if p is not None and p < 0.05:
                    any_sig = True
            else:
                cells.extend(["", "", ""])
        if not has_comparison or show_all or any_sig:
            table.add_row(*cells)
        else:
            omitted += 1

    console = Console(
        no_color=not ansi, highlight=False, width=200, force_terminal=ansi
    )
    with console.capture() as capture:
        console.print(table, end="")
    table_text = capture.get()

    title = ", ".join(lineitems) if lineitems else "full suite"
    lines = [f"{title}  ({suite}, {n} run{'s' if n > 1 else ''})"]
    lines.append(table_text)
    if omitted:
        d, r = ("\033[2m", "\033[0m") if ansi else ("", "")
        lines.append(
            f"{d}({omitted} non-significant result"
            f"{'s' if omitted != 1 else ''} omitted"
            f" — pass --show-all for all results){r}"
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

    # Attach p-values and confidence when there are exactly two variants
    if len(results) == 2:
        a, b = results
        for metric in a.keys() & b.keys():
            va, vb = a[metric], b[metric]
            if len(va) >= 2 and len(vb) >= 2:
                _, p = ttest_ind(va, vb, equal_var=False)
                p = float(p)
                for side in (0, 1):
                    out[side][metric]["p_value"] = round(p, 4)
                    out[side][metric]["confidence"] = _p_confidence(p)

    return out


# ---------- CLI ----------
