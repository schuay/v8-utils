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

from scipy.stats import ttest_ind

from . import config


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


def _p_confidence(p: float) -> str:
    """Map a p-value to a human-readable confidence level."""
    if p < 0.01:
        return "high"
    if p < 0.05:
        return "medium"
    return "low"


@dataclass(frozen=True)
class Stats:
    """One variant's runs of one metric."""

    values: list[float]
    mean: float
    #: 0.0 for a single run.
    stdev: float
    #: stdev as a percentage of the mean; 0.0 when the mean is 0.
    stdev_pct: float


@dataclass(frozen=True)
class Delta:
    """An experiment variant against the base variant, for one metric."""

    #: (experiment - base) / base, in percent; None when the base mean is 0.
    pct: float | None
    #: Welch's t-test; None with fewer than two runs on either side, or when
    #: pct is None.
    p_value: float | None
    #: "high", "medium" or "low" from the p-value; "" without one.
    confidence: str

    @property
    def significant(self) -> bool:
        return self.p_value is not None and self.p_value < 0.05


@dataclass(frozen=True)
class MetricComparison:
    metric: str
    #: Per variant, base first; None where the variant reported no value.
    stats: list[Stats | None]
    #: Per experiment variant; None where either side reported no value.
    deltas: list[Delta | None]


@dataclass(frozen=True)
class Comparison:
    """A JetStream run of one or more variants, analysed per metric."""

    lineitems: list[str] | None
    suite: str
    runs: int
    #: Variant labels, base first.
    labels: list[str]
    #: Suite aggregates first, then grouped by benchmark, Score leading.
    metrics: list[MetricComparison]


def stats(values: list[float]) -> Stats:
    m = mean(values)
    s = stdev(values) if len(values) > 1 else 0.0
    return Stats(values=values, mean=m, stdev=s, stdev_pct=100 * s / m if m else 0.0)


def delta(base: list[float], exp: list[float]) -> Delta:
    bm, em = mean(base), mean(exp)
    if bm == 0:
        return Delta(pct=None, p_value=None, confidence="")
    pct = 100 * (em - bm) / bm
    if len(base) >= 2 and len(exp) >= 2:
        _, p = ttest_ind(base, exp, equal_var=False)
        return Delta(pct=pct, p_value=float(p), confidence=_p_confidence(p))
    return Delta(pct=pct, p_value=None, confidence="")


def compare(
    lineitems: list[str] | None,
    suite: str,
    runs: int,
    variants: list[Variant],
    results: list[dict[str, list[float]]],
) -> Comparison:
    """Per-metric statistics for each variant, and each experiment variant
    against the first."""
    all_metrics: set[str] = set()
    for r in results:
        all_metrics.update(r.keys())
    metrics = []
    for metric in sorted(all_metrics, key=_metric_sort_key):
        base_vals = results[0].get(metric, [])
        per_variant = [stats(r[metric]) if r.get(metric) else None for r in results]
        deltas = [
            delta(base_vals, r[metric]) if base_vals and r.get(metric) else None
            for r in results[1:]
        ]
        metrics.append(MetricComparison(metric, per_variant, deltas))
    return Comparison(
        lineitems=lineitems,
        suite=suite,
        runs=runs,
        labels=[v.label for v in variants],
        metrics=metrics,
    )


def _bench_setup(binaries: list[str], suite: str):
    """Validate the suite checkout and the binaries; the config, suite directory,
    suite label, whether it is JetStream3, and the variants."""
    cfg = config.load()
    js3 = suite.lower() != "js2"
    key = "js3" if js3 else "js2"
    suite_dir = cfg.repos[key].path
    suite_label = "JS3" if js3 else "JS2"
    # Name the path, and name it HERE. The suite is reached as
    # `suite_dir/cli.js`, so a missing one otherwise surfaces as d8 failing to
    # open a file several layers down -- and a caller that cannot see the
    # configured path has no way to tell "not installed" from "installed
    # somewhere else". Sandboxed agents read that as "JetStream is missing"
    # and silently skip the measurement, which on a perf job is the whole
    # deliverable. The path is not guessable either (the real one is
    # `.../v8-perf/benchmarks/JetStream/v3.0-custom`, containing no "js3"),
    # so stating it is the difference between an actionable error and a dead
    # end.
    if not (suite_dir / "cli.js").is_file():
        raise FileNotFoundError(
            f"{suite_label} not found: no cli.js under {suite_dir} "
            f"(configured as repos.{key} in {config.CONFIG_PATH}). "
            "Point that at a JetStream checkout, or pass a suite that is "
            "installed."
        )

    for b in binaries:
        path_part = b.split(":")[0].strip()
        if not Path(path_part).is_absolute():
            raise ValueError(
                f"binary must be an absolute path, got {path_part!r}. "
                f"Example: /home/user/src/v8/v8/out/x64.release/d8"
            )
    variants = [Variant.parse(b) for b in binaries]
    for v in variants:
        d8 = v.d8(cfg.v8_out)
        if d8.is_dir():
            raise ValueError(
                f"{d8} is a directory, not a binary. "
                f'Pass the executable itself, e.g. "{d8}/d8".'
            )
        if not d8.exists():
            raise ValueError(f"binary not found: {d8}")

    return cfg, suite_dir, suite_label, js3, variants


def jsb_record(
    lineitems: list[str] | None,
    binaries: list[str],
    suite: str,
    record: str,
) -> str:
    """Record one binary's run of a JetStream story: "perf" or "perf_upload"
    returns the perf result, "v8log" the path of the recorded v8.log."""
    cfg, suite_dir, _, _, variants = _bench_setup(binaries, suite)
    _RECORD_MODES = ("perf", "perf_upload", "v8log")
    if record not in _RECORD_MODES:
        raise ValueError(f"record must be one of {_RECORD_MODES}, got {record!r}")
    if len(variants) != 1:
        raise ValueError("record mode requires exactly one binary")
    v = variants[0]
    if record == "v8log":
        return str(run_v8log(v, suite_dir, lineitems, cfg.v8_out))
    return run_perf(
        v,
        suite_dir,
        lineitems,
        cfg.v8_out,
        cfg.perf_script,
        upload=(record == "perf_upload"),
    )


def jsb_compare(
    lineitems: list[str] | None,
    binaries: list[str],
    runs: int = 5,
    suite: str = "js3",
) -> Comparison:
    """Run a JetStream2/3 story `runs` times per binary, round robin, and
    compare the variants per metric (Welch's t-test against the first)."""
    cfg, suite_dir, suite_label, js3, variants = _bench_setup(binaries, suite)
    results = run_round_robin(variants, suite_dir, lineitems, runs, js3, cfg.v8_out)
    return compare(lineitems, suite_label, runs, variants, results)
