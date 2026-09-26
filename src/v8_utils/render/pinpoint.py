"""Text for Pinpoint jobs and their results, with optional ANSI colour."""

import logging

from rich import box
from rich.console import Console
from rich.table import Table

from ..api import pinpoint

# ── ANSI escape codes ─────────────────────────────────────────────────────────
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


def results_header(job: dict, ansi: bool = False) -> str:
    """Build the header lines (bot/benchmark/patch/flags) for a results table."""
    experiment_patch_url = job.get("experiment_patch")
    experiment_patch_subject = pinpoint.subject_or_none(experiment_patch_url)
    base_patch_url = job.get("base_patch")
    base_patch_subject = pinpoint.subject_or_none(base_patch_url)
    base_hash = job.get("base_git_hash")
    base_flags = job.get("base_extra_args")
    exp_flags = job.get("experiment_extra_args")

    b, d, c, r = (_BOLD, _DIM, _CYAN, _RESET) if ansi else ("", "", "", "")

    lines: list[str] = []
    header_parts = []
    configuration = job.get("configuration")
    if configuration:
        header_parts.append(
            f"{d}bot:{r} {b}{pinpoint.short_configuration(configuration)}{r}"
        )
    benchmark = job.get("benchmark")
    story = job.get("story")
    if benchmark:
        bench_val = pinpoint.short_benchmark(benchmark)
        if story:
            bench_val += f" / {story}"
        header_parts.append(f"{d}benchmark:{r} {b}{bench_val}{r}")
    created = job.get("created")
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
    job_id: str,
    show_all: bool,
    use_cas: bool,
    compact: bool = False,
    job: dict | None = None,
    ansi: bool = False,
) -> str | None:
    """Format a results table for a single job. Returns None if no results.

    job:  pre-fetched job detail dict (avoids re-fetching for header).
    ansi: if True, embed ANSI escape codes for colored terminal output.

    Returns an error string (not raises) on failure so multi-job batches
    can continue.
    """
    try:
        all_rows = (
            pinpoint.pivot_results_cas(job_id)
            if use_cas
            else pinpoint.pivot_results(job_id)
        )
    except Exception as e:
        logging.getLogger("v8-utils").debug(
            "pivot_results failed for %s", job_id, exc_info=True
        )
        return f"Error: {e}"
    if not all_rows:
        return None

    rows = all_rows if show_all else [r for r in all_rows if r["significant"]]
    omitted = len(all_rows) - len(rows)
    if job is None:
        try:
            job = pinpoint.fetch_job_detail(job_id)
        except Exception:
            logging.getLogger("v8-utils").debug(
                "fetch_job_detail failed for %s", job_id, exc_info=True
            )
            job = {}

    d, r = (_DIM, _RESET) if ansi else ("", "")

    if not rows:
        header = results_header(job, ansi=ansi)
        no_sig = (
            f"{d}(no statistically significant results){r}"
            if ansi
            else "(no statistically significant results)"
        )
        return f"{header}\n{no_sig}" if header else no_sig

    def pct(row: dict) -> float:
        bm = row["base_mean"] or 0
        return (row["exp_mean"] - bm) / bm * 100 if bm else 0

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
        bm, bs = row["base_mean"] or 0, row["base_stdev"] or 0
        em, es = row["exp_mean"] or 0, row["exp_stdev"] or 0
        pct_str = f"{pct(row):+.2f}%"
        direction = _direction(row.get("unit"))
        style = _pct_style(pct_str, direction)
        cols: list[str] = [
            row["name"],
            f"{_rd(bm, 4)} ±{_rd(bs, 3)}",
            f"{_rd(em, 4)} ±{_rd(es, 3)}",
            f"[{style}]{pct_str}[/]",
            f"{row['p_value']:.4f}",
        ]
        if not compact:
            sig = "*" if row["significant"] else ""
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

    header = results_header(job, ansi=ansi)
    lines: list[str] = [header] if header else []
    lines.append(table_text)
    if omitted:
        omit_text = (
            f"({omitted} non-significant result{'s' if omitted != 1 else ''} omitted)"
        )
        lines.append(f"{d}{omit_text}{r}" if ansi else omit_text)
    return "\n".join(lines)


def format_job_detail(j: dict) -> str:
    """Format a job dict as compact text (mirrors pp's _print_job without ANSI)."""
    created = (j.get("created") or "")[:16].replace("T", " ")
    status = j.get("status") or "?"
    url = j.get("url") or ""

    patch_url = j.get("experiment_patch")
    patch_subject = pinpoint.subject_or_none(patch_url)

    lines = [f"{created}  {status}  {url}"]
    # Merged bot + benchmark line
    header_parts = []
    cfg = j.get("configuration")
    bench = j.get("benchmark")
    story = j.get("story")
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
        ("user", j.get("user")),
        ("mode", j.get("comparison_mode")),
        ("base", j.get("base_git_hash")),
        ("end", j.get("end_git_hash")),
        ("patch", patch_url),
        ("base-flags", j.get("base_extra_args")),
        ("exp-flags", j.get("experiment_extra_args")),
        ("diffs", j.get("difference_count")),
        ("bug", j.get("bug_id")),
        ("results", j.get("results_url")),
        ("exception", j.get("exception")),
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
