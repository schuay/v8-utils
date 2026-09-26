"""Text for Pinpoint jobs and their results, with optional ANSI colour."""

from rich import box
from rich.console import Console
from rich.table import Table

from ..api import pinpoint

# ── ANSI escape codes ─────────────────────────────────────────────────────────
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


def results_header(
    job: pinpoint.Job, subjects: dict[str, str | None] | None = None, ansi: bool = False
) -> str:
    """Build the header lines (bot/benchmark/patch/flags) for a results table.

    subjects: patch URL to subject, for the patches the job names.
    """
    subjects = subjects or {}
    experiment_patch_url = job.experiment_patch
    experiment_patch_subject = subjects.get(experiment_patch_url)
    base_patch_url = job.base_patch
    base_patch_subject = subjects.get(base_patch_url)
    base_hash = job.base_git_hash
    base_flags = job.base_extra_args
    exp_flags = job.experiment_extra_args

    b, d, c, r = (_BOLD, _DIM, _CYAN, _RESET) if ansi else ("", "", "", "")

    lines: list[str] = []
    header_parts = []
    configuration = job.configuration
    if configuration:
        header_parts.append(
            f"{d}bot:{r} {b}{pinpoint.short_configuration(configuration)}{r}"
        )
    benchmark = job.benchmark
    story = job.story
    if benchmark:
        bench_val = pinpoint.short_benchmark(benchmark)
        if story:
            bench_val += f" / {story}"
        header_parts.append(f"{d}benchmark:{r} {b}{bench_val}{r}")
    created = job.created
    if created:
        header_parts.append(f"{d}date:{r} {b}{created[:16].replace('T', ' ')}{r}")
    if header_parts:
        sep = f" {d}│{r} " if ansi else "  "
        lines.append(sep.join(header_parts))
    if base_hash:
        lines.append(f"{d}base:{r} {c}{base_hash}{r}")
    if base_patch_url:
        base_patch_line = f"{d}base-patch:{r} {c}{base_patch_url}{r}"
        if base_patch_subject:
            base_patch_line += f'  "{base_patch_subject}"'
        lines.append(base_patch_line)
    if experiment_patch_url:
        patch_line = f"{d}patch:{r} {c}{experiment_patch_url}{r}"
        if experiment_patch_subject:
            patch_line += f'  "{experiment_patch_subject}"'
        lines.append(patch_line)
    if base_flags:
        lines.append(f"{d}base-flags:{r} {c}{base_flags}{r}")
    if exp_flags:
        lines.append(f"{d}exp-flags:{r}  {c}{exp_flags}{r}")
    return "\n".join(lines)


def format_results_table(
    results: pinpoint.JobResults,
    show_all: bool,
    compact: bool = False,
    ansi: bool = False,
) -> str | None:
    """Format a results table for a single job. Returns None if no results.

    ansi: if True, embed ANSI escape codes for colored terminal output.

    A job whose results could not be read renders as its error, so a
    multi-job batch still shows the others.
    """
    if results.error is not None:
        return f"Error: {results.error}"
    all_rows = results.rows
    if not all_rows:
        return None

    rows = all_rows if show_all else [r for r in all_rows if r.significant]
    omitted = len(all_rows) - len(rows)
    job = results.job

    d, r = (_DIM, _RESET) if ansi else ("", "")

    if not rows:
        header = results_header(job, results.subjects, ansi=ansi)
        no_sig = (
            f"{d}(no statistically significant results){r}"
            if ansi
            else "(no statistically significant results)"
        )
        return f"{header}\n{no_sig}" if header else no_sig

    def pct(row: pinpoint.ResultRow) -> float:
        bm = row.base_mean or 0
        return (row.exp_mean - bm) / bm * 100 if bm else 0

    rows.sort(key=pct, reverse=True)

    def _direction(unit: str | None) -> str:
        if unit and "biggerIsBetter" in unit:
            return "bigger-better"
        if unit and "smallerIsBetter" in unit:
            return "smaller-better"
        return ""

    def _rd(v: float, min_digits: int) -> str:
        pre = len(str(int(abs(v))))
        post = max(0, min_digits - pre)
        return f"{v:.{post}f}"

    def _pct_style(pct_str: str, direction: str) -> str:
        inverted = direction == "smaller-better"
        good = pct_str.startswith("-") if inverted else pct_str.startswith("+")
        return "green" if good else "red"

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold", padding=(0, 1))
    table.add_column("metric")
    table.add_column("base±std", justify="right")
    table.add_column("exp±std", justify="right")
    table.add_column("chg%", justify="right")
    table.add_column("p", justify="right")
    if not compact:
        table.add_column("sig", justify="right")
        table.add_column("direction")

    for row in rows:
        bm, bs = row.base_mean or 0, row.base_stdev or 0
        em, es = row.exp_mean or 0, row.exp_stdev or 0
        pct_str = f"{pct(row):+.2f}%"
        direction = _direction(row.unit)
        style = _pct_style(pct_str, direction)
        cols: list[str] = [
            row.name,
            f"{_rd(bm, 4)} ±{_rd(bs, 3)}",
            f"{_rd(em, 4)} ±{_rd(es, 3)}",
            f"[{style}]{pct_str}[/]",
            f"{row.p_value:.4f}",
        ]
        if not compact:
            sig = "*" if row.significant else ""
            cols.append(f"[bold green]{sig}[/]" if sig else "")
            cols.append(direction)
        table.add_row(*cols)

    # color_system is pinned rather than auto-detected: rich reads $TERM, and
    # under TERM=dumb it resolves to None and silently drops every color in the
    # table while results_header above still emits its escapes -- a half-colored
    # report.  The ansi flag is the caller's decision (pp passes stdout.isatty()),
    # so honour it and let the caller decide, as it already does for the header.
    console = Console(
        no_color=not ansi,
        highlight=False,
        width=200,
        force_terminal=ansi,
        color_system="standard" if ansi else None,
    )
    with console.capture() as capture:
        console.print(table, end="")
    table_text = capture.get()

    header = results_header(job, results.subjects, ansi=ansi)
    lines: list[str] = [header] if header else []
    lines.append(table_text)
    if omitted:
        omit_text = (
            f"({omitted} non-significant result{'s' if omitted != 1 else ''} omitted)"
        )
        lines.append(f"{d}{omit_text}{r}" if ansi else omit_text)
    return "\n".join(lines)


def format_job_detail(j: pinpoint.Job, patch_subject: str | None = None) -> str:
    """Format a job as compact text (mirrors pp's _print_job without ANSI).

    patch_subject: the subject of the job's experiment patch, if known.
    """
    created = (j.created or "")[:16].replace("T", " ")
    status = j.status or "?"
    url = j.url or ""

    patch_url = j.experiment_patch

    lines = [f"{created}  {status}  {url}"]
    # Merged bot + benchmark line
    header_parts = []
    cfg = j.configuration
    bench = j.benchmark
    story = j.story
    if cfg:
        header_parts.append(f"bot: {pinpoint.short_configuration(cfg)}")
    if bench:
        bench_str = f"benchmark: {pinpoint.short_benchmark(bench)}"
        if story:
            bench_str += f" / {story}"
        header_parts.append(bench_str)
    if header_parts:
        lines.append("  ".join(header_parts))
    fields = [
        ("user", j.user),
        ("mode", j.comparison_mode),
        ("base", j.base_git_hash),
        ("end", j.end_git_hash),
        ("patch", patch_url),
        ("base-flags", j.base_extra_args),
        ("exp-flags", j.experiment_extra_args),
        ("diffs", j.difference_count),
        ("bug", j.bug_id),
        ("results", j.results_url),
        ("exception", j.exception),
    ]
    w = max((len(k) for k, v in fields if v is not None), default=0)
    for key, val in fields:
        if val is None:
            continue
        if key == "patch" and patch_subject:
            val = f'{val}  "{patch_subject}"'
        lines.append(f"  {key:<{w}}  {val}")
    return "\n".join(lines)


def format_cancelled(c: pinpoint.Cancelled) -> str:
    """One line per cancelled job, as `pp cancel-job` and the MCP tool print it."""
    if c.error is not None:
        return f"Job {c.job_id}: Error: {c.error}"
    return f"Job {c.job_id}: {c.state}"
