"""Text for the samply_* analyses: fixed-width tables in the style of the
perf_* tools, headed by the thread and the time base they cover."""

import shlex
from pathlib import Path

from ..api import samply


def tier_label(f: samply.Function) -> str:
    """The JS tier with its code flavour, e.g. "turbofan osr"; empty for
    code that has no tier."""
    if f.tier is None:
        return ""
    return " ".join(
        [f.tier] + (["osr"] if f.osr else []) + (["ctx"] if f.specialized else [])
    )


def short_name(f: samply.Function) -> str:
    """The name without a C++ parameter list, for one-line views."""
    if f.kind != "native":
        return f.name
    depth = 0
    for i, c in enumerate(f.name):
        if c == "<":
            depth += 1
        elif c == ">":
            depth -= 1
        elif (
            c == "("
            and depth == 0
            and i > 0
            and not f.name.startswith("(anonymous namespace)", i)
            and not (f.name.startswith("()", i) and f.name.endswith("operator", 0, i))
        ):
            return f.name[:i]
    return f.name


def _basis(
    thread: samply.Thread, wall: bool, total_ms: float, time: str | None = None
) -> str:
    kind = "wall time" if wall else "CPU time"
    suffix = f", time {time}" if time else ""
    return f"thread {thread.name} (tid {thread.tid}), {kind} {total_ms:.0f} ms{suffix}"


def _pct(ms: float, total_ms: float) -> float:
    return 100 * ms / total_ms if total_ms else 0.0


def location(where: tuple[str, int] | None) -> str:
    """A source location as "dir/file.cc:line", or "" when unknown."""
    if where is None:
        return ""
    path, line = where
    return f"{'/'.join(Path(path).parts[-2:])}:{line}"


def _unsymbolized(profile: samply.Profile) -> list[str]:
    lines = []
    if profile.unsymbolized:
        lines += ["", "Not symbolized (frames named <lib>+<rva>):"]
        lines += [
            f"  {lib}: {why}" for lib, why in sorted(profile.unsymbolized.items())
        ]
    if profile.no_lines:
        lines += ["", "No line info or inline frames:"]
        lines += [f"  {lib}: {why}" for lib, why in sorted(profile.no_lines.items())]
    return lines


def format_recording_info(profile: samply.Profile) -> list[str]:
    """2-3 line provenance header for a recording: binary + build mtime/rev,
    recording time + interval + total samples, and command line."""
    info = profile.info
    lines: list[str] = []
    if info.binary:
        tags: list[str] = []
        if info.binary_mtime:
            tags.append(f"built {info.binary_mtime}")
        elif info.binary_status:
            tags.append(info.binary_status)
        if info.revision:
            tags.append(f"rev {info.revision}")
        detail = f" ({', '.join(tags)})" if tags else ""
        lines.append(f"binary: {info.binary}{detail}")
    hz = f" ({1000 / info.interval_ms:g} Hz)" if info.interval_ms else ""
    rec_parts = [
        *([f"recorded {info.recorded}"] if info.recorded else []),
        f"interval {info.interval_ms:g} ms{hz}",
        f"{info.samples} samples",
    ]
    lines.append(", ".join(rec_parts))
    if info.cmdline:
        rec_dir = str(profile.path.parent)
        short = [
            a.replace(f"={rec_dir}", "=.") if rec_dir != "." else a
            for a in info.cmdline
        ]
        lines.append(f"cmd: {shlex.join(short)}")
    return lines


def format_hotspots(profile: samply.Profile, h: samply.Hotspots) -> str:
    n_samples = h.samples if h.samples is not None else h.thread.sample_count()
    basis = f"{_basis(h.thread, h.wall, h.total_ms, h.time)}, {n_samples} samples"
    if not h.rows:
        return "\n".join([*format_recording_info(profile), f"No samples ({basis})."])
    idx_w = len(str(len(h.rows)))
    lib_w = max(len("lib"), *(len(r.function.lib or "") for r in h.rows))
    lines = [*format_recording_info(profile), basis, ""]
    header = (
        f"{'#':>{idx_w}}  {'self%':>6}  {'total%':>6}  {'self ms':>8}"
        f"  {'kind':<7}  {'tier':<15}  {'lib':<{lib_w}}  function"
    )
    lines += [header, "-" * len(header)]
    for i, r in enumerate(h.rows, 1):
        f = r.function
        lines.append(
            f"{i:>{idx_w}}  {_pct(r.self_ms, h.total_ms):5.1f}%"
            f"  {_pct(r.total_ms, h.total_ms):5.1f}%  {r.self_ms:8.1f}"
            f"  {f.kind:<7}  {tier_label(f):<15}  {f.lib or '':<{lib_w}}  {f.name}"
            + (f"  ({location(f.defined_at)})" if f.defined_at else "")
        )
    return "\n".join(lines + _unsymbolized(profile))


def _short(f: samply.Function) -> str:
    tier = tier_label(f)
    return f"{short_name(f)} [{tier}]" if tier else short_name(f)


def format_callers(c: samply.Callers) -> str:
    lines = [
        _basis(c.thread, c.wall, c.total_ms, c.time),
        f"matched: {', '.join(sorted(c.matched))}",
        (
            f"inclusive: {c.inclusive_ms:.1f} ms"
            f" ({_pct(c.inclusive_ms, c.total_ms):.1f}% of the thread)"
        ),
        "",
    ]
    for p in c.paths:
        share = _pct(p.ms, c.inclusive_ms)
        chain = (
            " <- ".join(
                _short(f) + (f" ({location(src)})" if src else "")
                for f, src in zip(p.callers, p.sources, strict=True)
            )
            or "(no callers)"
        )
        lines.append(f"{share:5.1f}%  {p.ms:8.1f} ms  <- {chain}")
    return "\n".join(lines)


def format_flamegraph(fg: samply.Flamegraph) -> str:
    """Collapsed stacks, "root;...;leaf <ms>", heaviest first."""
    lines = [_basis(fg.thread, fg.wall, fg.total_ms, fg.time)]
    if fg.focus is not None:
        lines.append(f"focus: {', '.join(sorted(fg.focus))}")
    lines.append("")
    for p in fg.paths:
        lines.append(
            f"{_pct(p.ms, fg.total_ms):5.1f}%  "
            + ";".join(
                _short(f) + (" [inl]" if inl else "")
                for f, inl in zip(p.frames, p.inlined, strict=True)
            )
            + f" {p.ms:.1f}"
        )
    if not fg.paths:
        lines.append("No paths above the cutoff.")
    return "\n".join(lines)


def format_diff(rows: list[samply.DiffRow]) -> str:
    if not rows:
        return "No differences."
    header = f"{'delta':>8}  {'base%':>6}  {'after%':>7}  {'kind':<7}  function"
    lines = [header, "-" * len(header)]
    for r in rows:
        base = f"{r.base_pct:.1f}%" if r.base_pct is not None else "new"
        after = f"{r.after_pct:.1f}%" if r.after_pct is not None else "gone"
        delta = (r.after_pct or 0.0) - (r.base_pct or 0.0)
        lines.append(
            f"{delta:+7.1f}%  {base:>6}  {after:>7}  {r.function.kind:<7}"
            f"  {_short(r.function)}"
        )
    return "\n".join(lines)


def format_threads(
    summary: list[samply.ThreadSummary], profile: samply.Profile | None = None
) -> str:
    header = (
        f"{'tid':>9}  {'wall ms':>8}  {'cpu ms':>8}  {'cpu/wall':>8}  {'thread':<16}"
        "  hottest (self CPU)"
    )
    lines = [*format_recording_info(profile), ""] if profile is not None else []
    lines += [header, "-" * len(header)]
    for s in summary:
        ratio = f"{100 * s.cpu_ms / s.wall_ms:.0f}%" if s.wall_ms else "-"
        top = ", ".join(f"{_short(h.function)} {h.self_ms:.0f}ms" for h in s.top)
        lines.append(
            f"{s.thread.tid:>9}  {s.wall_ms:8.0f}  {s.cpu_ms:8.1f}  {ratio:>8}"
            f"  {s.thread.name:<16}  {top}"
        )
    return "\n".join(lines)


def format_slices(profile: samply.Profile, sl: samply.Slices) -> str:
    kind = "wall ms" if sl.wall else "CPU ms"
    span0, span1 = sl.span_ms
    lines = [
        *format_recording_info(profile),
        (
            f"thread {sl.thread.name} (tid {sl.thread.tid}), span"
            f" {span0:.1f}..{span1:.1f} ms ({span1 - span0:.1f} ms)"
        ),
    ]
    if sl.note:
        lines.append(sl.note)
    lines.append("")
    if not sl.slices:
        lines.append("No slices.")
        return "\n".join(lines)
    name_w = max(len("slice"), *(len(s.name) for s in sl.slices))
    header = (
        f"{'slice':<{name_w}}  {'win':>4}  {'samples':>7}  {kind:>8}  hottest (self)"
    )
    lines += [header, "-" * len(header)]
    for s in sl.slices:
        lines.append(
            f"{s.name:<{name_w}}  {s.count:>4}  {s.samples:>7}  {s.ms:8.1f}  {s.top}"
        )
    lines += ["", "time args:"]
    for s in sl.slices:
        arg = s.time_arg if len(s.time_arg) <= 100 else s.time_arg[:97] + "..."
        lines.append(f"  {s.name:<{name_w}}  {arg}")
    return "\n".join(lines)


def _annotation_header(a: samply.Annotation) -> list[str]:
    chosen = a.objects[a.selected]
    lines = [
        _basis(a.thread, a.wall, sum(o.ms for o in a.objects), a.time)
        + f" in {', '.join(sorted(a.matched))}",
        "",
        "Code objects (pass code=N to annotate another):",
    ]
    for i, o in enumerate(a.objects, 1):
        mark = "*" if i == a.selected + 1 else " "
        tier = tier_label(o.function)
        lines.append(
            f" {mark}{i:>3}  {o.ms:8.1f} ms  {tier + '  ' if tier else ''}{o.label}"
        )
    lines.append("")
    lines.append(f"code={a.selected + 1}: {chosen.ms:.1f} ms, {len(a.lines)} lines")
    if a.unplaced_ms:
        lines.append(
            f"warning: {a.unplaced_ms:.1f} ms of samples fell on no instruction"
            " boundary of this listing"
        )
    return lines


def _source_line(line: samply.AnnotatedLine) -> str:
    if not line.chain:
        return line.text
    inner = line.chain[0]
    where = location((inner.file, inner.line) if inner.line else None)
    names = " <- ".join(
        short_name(samply.Function(f.function, "native", None)) for f in line.chain
    )
    return "  ".join(part for part in (where, names, line.text) if part)


def _listing_line(a: samply.Annotation, i: int) -> str:
    line = a.lines[i]
    if line.address is None:
        return f"{i + 1:6}  {'':6}  {'':12}  ; {_source_line(line)}"
    total = a.objects[a.selected].ms
    pct = f"{_pct(line.ms, total):5.1f}%" if line.ms else ""
    return f"{i + 1:6}  {pct:>6}  {line.address:12x}  {line.text}"


def format_annotation(
    a: samply.Annotation, min_pct: float = 0.5, context: int = 8, top: int = 20
) -> str:
    """The hottest instructions, then each block of instructions at or above
    min_pct, widened by `context` lines and ordered by peak heat. Line
    numbers index the full listing, for samply_annotate_read_around."""
    total = a.objects[a.selected].ms
    out = _annotation_header(a)
    hot = sorted(
        (i for i, line in enumerate(a.lines) if line.ms),
        key=lambda i: -a.lines[i].ms,
    )
    out += ["", "Top instructions:", f"{'line':>6}  {'pct':>6}  {'address':>12}  asm"]
    out += [_listing_line(a, i) for i in hot[:top]]

    threshold = total * min_pct / 100
    blocks: list[list[int]] = []
    for i in sorted(i for i in hot if a.lines[i].ms >= threshold):
        lo, hi = max(0, i - context), min(len(a.lines) - 1, i + context)
        if blocks and lo <= blocks[-1][1] + 1:
            blocks[-1][1] = max(blocks[-1][1], hi)
        else:
            blocks.append([lo, hi])
    blocks.sort(key=lambda b: -max(a.lines[i].ms for i in range(b[0], b[1] + 1)))
    for n, (lo, hi) in enumerate(blocks, 1):
        peak = max(a.lines[i].ms for i in range(lo, hi + 1))
        out += [
            "",
            f"Hot block #{n} (lines {lo + 1}-{hi + 1}, peak {_pct(peak, total):.1f}%):",
        ]
        out += [_listing_line(a, i) for i in range(lo, hi + 1)]
    return "\n".join(out)


def format_read_around(a: samply.Annotation, line: int, context: int = 30) -> str:
    """Lines line-context .. line+context of the listing (1-based)."""
    if not 1 <= line <= len(a.lines):
        raise ValueError(f"line must be 1..{len(a.lines)}")
    lo, hi = max(0, line - 1 - context), min(len(a.lines), line + context)
    out = _annotation_header(a) + [""]
    return "\n".join(out + [_listing_line(a, i) for i in range(lo, hi)])
