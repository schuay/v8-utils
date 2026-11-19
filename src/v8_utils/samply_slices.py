"""Time slicing and JS3 iteration-bucket analysis for samply profiles.

Supports three time-spec forms:
  - Explicit ms/s ranges: "100,250", "100ms,250ms", "0.1s,0.3s", ",200", "100,",
    or multiple disjoint ranges separated by whitespace or ";".
  - Percentage of the thread's sample span: "0%,20%", "0%,10% 80%,100%".
  - Named iteration slices ("first", "worst", "best", "average", "iters"),
    resolved from native samply markers (`*-iteration-*`) or a `timings.tsv`
    (+ optional `iterations.json`) sidecar next to `profile.json.gz`.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .samply import Profile, Thread

_ITERATION_RE = re.compile(r"^(.*)-iteration-(\d+)$")
NAMED_SLICES = frozenset({"first", "worst", "best", "average", "iters"})


@dataclass(frozen=True)
class Slice:
    name: str
    ranges: tuple[tuple[float, float], ...]
    time_arg: str
    samples: int
    ms: float
    top: str
    count: int = 1  # number of iterations / windows in this slice


@dataclass(frozen=True)
class Slices:
    thread: Thread
    wall: bool
    span_ms: tuple[float, float]
    source: str  # "markers", "timings.tsv", or "equal-time"
    slices: list[Slice]
    note: str | None = None


def merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        prev_s, prev_e = merged[-1]
        if start <= prev_e:
            merged[-1] = (prev_s, max(prev_e, end))
        else:
            merged.append((start, end))
    return merged


def format_ranges(ranges: list[tuple[float, float]]) -> str:
    return " ".join(f"{s:.1f},{e:.1f}" for s, e in ranges)


def _extract_marker_iterations(thread: Thread) -> list[tuple[int, float, float]]:
    """Iteration (index, start_ms, end_ms) from native samply markers."""
    m = thread._markers
    length = m.get("length") or 0
    if not length:
        return []
    names = m.get("name") or []
    starts = m.get("startTime") or []
    ends = m.get("endTime") or []
    strings = thread._string_array
    out: list[tuple[int, float, float]] = []
    for i in range(length):
        name_idx = names[i]
        if not (0 <= name_idx < len(strings)):
            continue
        match = _ITERATION_RE.match(strings[name_idx])
        if not match:
            continue
        s, e = starts[i], ends[i]
        if s is None or e is None:
            continue
        out.append((int(match.group(2)), float(s), float(e)))
    out.sort(key=lambda x: x[0])
    return out


def _extract_sidecar_iterations(
    profile: Profile,
) -> tuple[list[tuple[int, float, float]], int]:
    """Iteration (index, start_ms, end_ms) + worstCaseCount from sidecar
    `timings.tsv` (+ optional `iterations.json`) next to profile.json.gz."""
    rec_dir = profile.path.parent
    timings = rec_dir / "timings.tsv"
    if not timings.is_file():
        return ([], 4)

    # Optional macOS clock anchor: `# anchor <wall_epoch_us> <time_ticks_us>`
    # converts absolute TimeTicks µs into ms relative to profile.start_time_ms.
    offset_ms: float | None = None
    raw_rows: list[tuple[int, float, float]] = []
    for line in timings.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line.lstrip("#").split()
            if len(parts) == 3 and parts[0] == "anchor":
                wall_us = float(parts[1])
                ticks_us = float(parts[2])
                offset_ms = (wall_us / 1000.0 - profile.start_time_ms) - (
                    ticks_us / 1000.0
                )
            continue
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        match = _ITERATION_RE.match(cols[0])
        if not match:
            continue
        raw_rows.append(
            (int(match.group(2)), float(cols[1]) / 1000.0, float(cols[2]) / 1000.0)
        )

    rows = (
        [(idx, s + offset_ms, e + offset_ms) for idx, s, e in raw_rows]
        if offset_ms is not None
        else raw_rows
    )
    rows.sort(key=lambda x: x[0])

    worst_count = 4
    iters_json = rec_dir / "iterations.json"
    if iters_json.is_file():
        try:
            meta = json.loads(iters_json.read_text())
            if isinstance(meta, dict) and isinstance(meta.get("worstCaseCount"), int):
                worst_count = max(1, meta["worstCaseCount"])
        except (OSError, ValueError):
            pass
    return (rows, worst_count)


def _iteration_buckets(
    profile: Profile, thread: Thread, min_iteration: int = 1
) -> tuple[str, dict[str, list[tuple[float, float]]], int] | None:
    """Named JS3 buckets from markers or sidecar timings, or None if absent."""
    iters = _extract_marker_iterations(thread)
    source = "markers"
    worst_count = 4
    if not iters:
        iters, worst_count = _extract_sidecar_iterations(profile)
        source = "timings.tsv"
    if not iters:
        return None

    by_idx = {idx: (s, e) for idx, s, e in iters}
    buckets: dict[str, list[tuple[float, float]]] = {}
    if 0 in by_idx:
        buckets["first"] = [by_idx[0]]
    steady = [(idx, s, e, e - s) for idx, s, e in iters if idx >= min_iteration]
    if steady:
        k = min(worst_count, len(steady))
        worst = sorted(steady, key=lambda x: (-x[3], x[0]))[:k]
        best = sorted(steady, key=lambda x: (x[3], x[0]))[:k]
        buckets["worst"] = merge_ranges([(s, e) for _, s, e, _ in worst])
        buckets["best"] = merge_ranges([(s, e) for _, s, e, _ in best])
        buckets["average"] = merge_ranges([(s, e) for _, s, e, _ in steady])
    buckets["iters"] = merge_ranges([(s, e) for _, s, e in iters])
    return (source, buckets, len(iters))


def _parse_bound(token: str, t_min: float, t_max: float) -> float:
    tok = token.strip().lower()
    if tok.endswith("%"):
        pct = float(tok[:-1])
        if not 0.0 <= pct <= 100.0:
            raise ValueError(f"percentage time bound must be 0..100%, got {token!r}")
        return t_min + (t_max - t_min) * (pct / 100.0)
    if tok.endswith("ms"):
        return float(tok[:-2])
    if tok.endswith("s"):
        return float(tok[:-1]) * 1000.0
    return float(tok)


def resolve_time(
    profile: Profile,
    thread: Thread,
    time: str | None,
    *,
    min_iteration: int = 1,
) -> list[tuple[float, float]] | None:
    """Resolve a time specification to sorted disjoint (start_ms, end_ms) ranges."""
    if time is None:
        return None
    spec = time.strip()
    if not spec:
        return None
    low = spec.lower()
    if low in NAMED_SLICES:
        found = _iteration_buckets(profile, thread, min_iteration=min_iteration)
        if found is None:
            raise ValueError(
                f"no iteration timings in {profile.path.parent}; cannot resolve"
                f" slice {spec!r}. Use ms or % ranges (e.g. '0%,20%' or"
                " '100,250'), or call samply_slices to inspect the timeline."
            )
        _, buckets, _ = found
        if low not in buckets:
            raise ValueError(
                f"slice {spec!r} not available (available: {', '.join(sorted(buckets))})"
            )
        return buckets[low]

    t_min, t_max = thread.time_span()
    ranges: list[tuple[float, float]] = []
    for part in re.split(r"[\s;]+", spec):
        if not part:
            continue
        if "," not in part:
            raise ValueError(
                f"invalid time range {part!r}: expected 'start,end' (ms, s, or %)"
                f" or a slice name ({', '.join(sorted(NAMED_SLICES))})"
            )
        left, right = part.split(",", 1)
        start = _parse_bound(left, t_min, t_max) if left.strip() else t_min
        end = _parse_bound(right, t_min, t_max) if right.strip() else t_max
        if end < start:
            raise ValueError(f"time range end < start in {part!r}")
        ranges.append((start, end))
    return merge_ranges(ranges)


def build_slices(
    profile: Profile,
    thread: Thread,
    *,
    buckets: int = 5,
    min_iteration: int = 1,
    wall: bool = False,
) -> Slices:
    """Compute iteration buckets (when markers/timings.tsv exist) or `buckets`
    equal-time slices across `thread`'s span."""
    if buckets < 1:
        raise ValueError(f"buckets must be >= 1, got {buckets}")
    if min_iteration < 0:
        raise ValueError(f"min_iteration must be >= 0, got {min_iteration}")
    t_min, t_max = thread.time_span()

    def summarize(
        name: str,
        ranges: list[tuple[float, float]],
        count: int = 1,
        *,
        display_ranges: list[tuple[float, float]] | None = None,
    ) -> Slice:
        self_ms: dict[str, float] = {}
        total = 0.0
        n_samples = 0
        for s in thread.samples(ranges):
            if s.weight:
                n_samples += 1
            w = s.weight * profile.interval_ms if wall else s.cpu_us / 1000.0
            total += w
            if s.frames and w:
                fn = s.frames[0].name
                self_ms[fn] = self_ms.get(fn, 0.0) + w
        if self_ms and total:
            top_fn, top_ms = max(self_ms.items(), key=lambda kv: kv[1])
            top = f"{top_fn} ({100 * top_ms / total:.1f}%)"
        else:
            top = "-"
        shown = display_ranges if display_ranges is not None else ranges
        return Slice(
            name=name,
            ranges=tuple(shown),
            time_arg=format_ranges(shown),
            samples=n_samples,
            ms=total,
            top=top,
            count=count,
        )

    found = _iteration_buckets(profile, thread, min_iteration=min_iteration)
    if found is not None:
        source, named, n_iters = found
        order = [
            k for k in ("first", "worst", "average", "best", "iters") if k in named
        ]
        out = [summarize(name, named[name], len(named[name])) for name in order]
        note = f"{n_iters} iterations from {source} (min_iteration={min_iteration})"
        return Slices(thread, wall, (t_min, t_max), source, out, note)

    span = t_max - t_min
    out = []
    if span > 0:
        step = span / buckets
        for i in range(buckets):
            s = t_min + i * step
            e = t_max if i == buckets - 1 else t_min + (i + 1) * step
            # Half-open [s, e) for non-final buckets prevents double-counting
            # a sample landing on an exact boundary.
            query_e = e if i == buckets - 1 else math.nextafter(e, s)
            p0 = round(100 * i / buckets)
            p1 = round(100 * (i + 1) / buckets)
            out.append(
                summarize(
                    f"{p0}%..{p1}%",
                    [(s, query_e)],
                    1,
                    display_ranges=[(s, e)],
                )
            )
    note = (
        "no iteration markers or timings.tsv; showing equal-time slices"
        " (pass time='start,end' or time='0%,20%' to samply_* tools)"
    )
    return Slices(thread, wall, (t_min, t_max), "equal-time", out, note)
