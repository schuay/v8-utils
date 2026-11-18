"""Text for the samply_* analyses: fixed-width tables in the style of the
perf_* tools, headed by the thread and the time base they cover."""

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


def _basis(thread: samply.Thread, wall: bool, total_ms: float) -> str:
    kind = "wall time" if wall else "CPU time"
    return f"thread {thread.name} (tid {thread.tid}), {kind} {total_ms:.0f} ms"


def _pct(ms: float, total_ms: float) -> float:
    return 100 * ms / total_ms if total_ms else 0.0


def _unsymbolized(profile: samply.Profile) -> list[str]:
    if not profile.unsymbolized:
        return []
    lines = ["", "Not symbolized (frames named <lib>+<rva>):"]
    lines += [f"  {lib}: {why}" for lib, why in sorted(profile.unsymbolized.items())]
    return lines


def format_hotspots(profile: samply.Profile, h: samply.Hotspots) -> str:
    if not h.rows:
        return f"No samples ({_basis(h.thread, h.wall, h.total_ms)})."
    idx_w = len(str(len(h.rows)))
    lib_w = max(len("lib"), *(len(r.function.lib or "") for r in h.rows))
    lines = [_basis(h.thread, h.wall, h.total_ms), ""]
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
        )
    return "\n".join(lines + _unsymbolized(profile))


def _short(f: samply.Function) -> str:
    tier = tier_label(f)
    return f"{short_name(f)} [{tier}]" if tier else short_name(f)


def format_callers(c: samply.Callers) -> str:
    lines = [
        _basis(c.thread, c.wall, c.total_ms),
        f"matched: {', '.join(sorted(c.matched))}",
        (
            f"inclusive: {c.inclusive_ms:.1f} ms"
            f" ({_pct(c.inclusive_ms, c.total_ms):.1f}% of the thread)"
        ),
        "",
    ]
    for p in c.paths:
        share = _pct(p.ms, c.inclusive_ms)
        chain = " <- ".join(_short(f) for f in p.callers) or "(no callers)"
        lines.append(f"{share:5.1f}%  {p.ms:8.1f} ms  <- {chain}")
    return "\n".join(lines)


def format_flamegraph(fg: samply.Flamegraph) -> str:
    """Collapsed stacks, "root;...;leaf <ms>", heaviest first."""
    lines = [_basis(fg.thread, fg.wall, fg.total_ms)]
    if fg.focus is not None:
        lines.append(f"focus: {', '.join(sorted(fg.focus))}")
    lines.append("")
    for p in fg.paths:
        lines.append(
            f"{_pct(p.ms, fg.total_ms):5.1f}%  "
            + ";".join(_short(f) for f in p.frames)
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


def format_threads(summary: list[samply.ThreadSummary]) -> str:
    header = (
        f"{'tid':>9}  {'wall ms':>8}  {'cpu ms':>8}  {'cpu/wall':>8}  {'thread':<16}"
        "  hottest (self CPU)"
    )
    lines = [header, "-" * len(header)]
    for s in summary:
        ratio = f"{100 * s.cpu_ms / s.wall_ms:.0f}%" if s.wall_ms else "-"
        top = ", ".join(f"{_short(h.function)} {h.self_ms:.0f}ms" for h in s.top)
        lines.append(
            f"{s.thread.tid:>9}  {s.wall_ms:8.0f}  {s.cpu_ms:8.1f}  {ratio:>8}"
            f"  {s.thread.name:<16}  {top}"
        )
    return "\n".join(lines)


def _annotation_header(a: samply.Annotation) -> list[str]:
    chosen = a.objects[a.selected]
    lines = [
        _basis(a.thread, a.wall, sum(o.ms for o in a.objects))
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


def _listing_line(a: samply.Annotation, i: int) -> str:
    line = a.lines[i]
    if line.address is None:
        return f"{i + 1:6}  {'':6}  {'':12}  ; {line.text}"
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
