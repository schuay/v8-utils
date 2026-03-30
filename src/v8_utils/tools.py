"""Shared tool implementations for v8-utils (used by both CLI and MCP)."""

import concurrent.futures
import logging
import subprocess
from collections.abc import Callable
from datetime import datetime

from rich import box
from rich.console import Console
from rich.table import Table

from . import config
from . import pinpoint

# ── ANSI escape codes ─────────────────────────────────────────────────────────
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


def _run_concurrent(
    fns: list[Callable[[], object]],
    on_progress: Callable[[int, int], None] | None = None,
) -> list:
    """Run callables concurrently, returning results in input order.

    on_progress(done, total) is called after each completion.
    Ctrl-C cancels pending futures and re-raises KeyboardInterrupt.
    """
    if len(fns) <= 1:
        return [fn() for fn in fns]
    with concurrent.futures.ThreadPoolExecutor() as ex:
        future_to_idx = {ex.submit(fn): i for i, fn in enumerate(fns)}
        results = [None] * len(fns)
        try:
            done = 0
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                results[idx] = future.result()
                done += 1
                if on_progress:
                    on_progress(done, len(fns))
        except KeyboardInterrupt:
            ex.shutdown(wait=False, cancel_futures=True)
            raise
    return results


def _fetch_job_details_sorted(
    job_ids: list[str],
    on_progress: Callable[[int, int], None] | None = None,
) -> list[tuple[str, dict]]:
    """Fetch job details in parallel, deduplicate, sort oldest-first.

    Returns [(job_id, detail_dict), ...].  On fetch error the dict
    contains an ``"error"`` key instead of normal fields.
    """
    job_ids = list(dict.fromkeys(job_ids))

    log = logging.getLogger("v8-utils")

    def fetch(jid: str) -> dict:
        try:
            return _fetch_job_detail(jid)
        except Exception as e:
            log.debug("fetch_job_detail failed for %s", jid, exc_info=True)
            return {"job_id": jid, "error": str(e)}

    fns = [lambda jid=jid: fetch(jid) for jid in job_ids]
    details = _run_concurrent(fns, on_progress)
    paired = list(zip(job_ids, details))
    paired.sort(key=lambda p: p[1].get("created") or "")
    return paired


def _fetch_job_detail(job_url: str) -> dict:
    """Fetch job details as a dict (internal helper)."""
    job_id = pinpoint.job_id_from_url(job_url)
    data = pinpoint.fetch_job(job_id)
    args = data.get("arguments", {})
    result = {
        "job_id": data.get("job_id"),
        "url": f"https://pinpoint-dot-chromeperf.appspot.com/job/{job_id}",
        "name": data.get("name"),
        "status": data.get("status"),
        "user": data.get("user"),
        "created": data.get("created"),
        "updated": data.get("updated"),
        "comparison_mode": data.get("comparison_mode"),
        "configuration": data.get("configuration"),
        "benchmark": args.get("benchmark"),
        "story": args.get("story"),
        "base_git_hash": args.get("base_git_hash"),
        "end_git_hash": args.get("end_git_hash"),
        "experiment_patch": args.get("experiment_patch"),
        "base_extra_args": args.get("base_extra_args"),
        "experiment_extra_args": args.get("experiment_extra_args"),
        "difference_count": data.get("difference_count"),
        "exception": data.get("exception"),
        "bug_id": data.get("bug_id"),
        "results_url": data.get("results_url"),
    }
    return {k: v for k, v in result.items() if v is not None}


def _fetch_jobs_list(
    count: int = 20,
    user: str | None = None,
    filters: list[str] | None = None,
    since: datetime | None = None,
) -> list[dict]:
    """Fetch job list as dicts (internal helper)."""
    if user is None:
        user = config.load().user or pinpoint.get_current_user_email()
    return [
        pinpoint.summarise_job(j)
        for j in pinpoint.fetch_jobs(user, count, filters, since=since)
    ]


def _results_header(job: dict, ansi: bool = False) -> str:
    """Build the header lines (bot/benchmark/patch/flags) for a results table."""
    patch_url = job.get("experiment_patch")
    patch_subject = pinpoint.fetch_gerrit_subject(patch_url) if patch_url else None
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
    if patch_url:
        patch_line = f"{d}patch:{r} {c}{patch_url}{r}"
        if patch_subject:
            patch_line += f'  "{patch_subject}"'
        lines.append(patch_line)
    if base_flags:
        lines.append(f"{d}base-flags:{r} {c}{base_flags}{r}")
    if exp_flags:
        lines.append(f"{d}exp-flags:{r}  {c}{exp_flags}{r}")
    return "\n".join(lines)


def _format_results_table(
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
            job = _fetch_job_detail(job_id)
        except Exception:
            logging.getLogger("v8-utils").debug(
                "fetch_job_detail failed for %s", job_id, exc_info=True
            )
            job = {}

    d, r = (_DIM, _RESET) if ansi else ("", "")

    if not rows:
        header = _results_header(job, ansi=ansi)
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

    console = Console(
        no_color=not ansi, highlight=False, width=200, force_terminal=ansi
    )
    with console.capture() as capture:
        console.print(table, end="")
    table_text = capture.get()

    header = _results_header(job, ansi=ansi)
    lines: list[str] = [header] if header else []
    lines.append(table_text)
    if omitted:
        omit_text = (
            f"({omitted} non-significant result{'s' if omitted != 1 else ''} omitted)"
        )
        lines.append(f"{d}{omit_text}{r}" if ansi else omit_text)
    return "\n".join(lines)


def _format_job_detail(j: dict) -> str:
    """Format a job dict as compact text (mirrors pp's _print_job without ANSI)."""
    created = (j.get("created") or "")[:16].replace("T", " ")
    status = j.get("status") or "?"
    url = j.get("url") or ""

    patch_url = j.get("experiment_patch")
    patch_subject = pinpoint.fetch_gerrit_subject(patch_url) if patch_url else None

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


def get_gerrit_issue_url(cwd: str | None = None) -> str | None:
    """Read the Gerrit CL URL for the current git branch from git config.

    Returns a full URL including patchset, e.g.:
      https://chromium-review.googlesource.com/7650974/1
    Returns None if not inside a git repo or the branch has no associated CL.
    """

    def _git(*args: str) -> str:
        r = subprocess.run(
            ["git"] + list(args), capture_output=True, text=True, cwd=cwd
        )
        return r.stdout.strip() if r.returncode == 0 else ""

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":
        return None
    issue = _git("config", f"branch.{branch}.gerritissue")
    if not issue:
        return None
    server = (
        _git("config", f"branch.{branch}.gerritserver")
        or "https://chromium-review.googlesource.com"
    )
    patchset = _git("config", f"branch.{branch}.gerritpatchset")
    url = f"{server}/{issue}"
    return f"{url}/{patchset}" if patchset else url


def chat_notify_watching(job_url: str) -> None:
    """Send a 'Watching' notification to Google Chat if configured."""
    cfg = config.load()
    if cfg.chat_app_space and cfg.chat_service_account_email:
        try:
            from . import chat

            chat.notify(
                cfg.chat_app_space,
                cfg.chat_service_account_email,
                f"\U0001f440 Watching: {job_url}",
            )
        except Exception:
            pass
    elif cfg.chat_webhook:
        try:
            import httpx

            httpx.post(
                cfg.chat_webhook,
                json={"text": f"\U0001f440 Watching: {job_url}"},
                timeout=10,
            )
        except Exception:
            pass


def _resolve_patch_sentinel(value: str, cwd: str | None = None) -> str | None:
    """Resolve a single patch sentinel: "auto" -> detect from branch, "none" -> None.

    Returns the resolved URL string, None (for "none"), or the original value.
    Raises ValueError if "auto" is used but no CL is found on the current branch.
    """
    if value.lower() == "none":
        return None
    if value.lower() == "auto":
        detected = get_gerrit_issue_url(cwd=cwd)
        if detected is None:
            branch = (
                subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                ).stdout.strip()
                or "(unknown)"
            )
            raise ValueError(
                f"No Gerrit CL found on the current branch ({branch}).\n"
                f"Either:\n"
                f"  - pass --patch with an explicit CL URL\n"
                f"  - pass --patch=none to clear the filter"
            )
        return detected
    return value


def resolve_patch_filter(value: str | None) -> str | None:
    """Resolve a --patch filter value, supporting "auto" and "none" sentinels.

    Returns the resolved URL string, or None (for "none" or None input).
    """
    if value is None:
        return None
    return _resolve_patch_sentinel(value)


def resolve_exp_patches(
    exp_patches: list[str], cwd: str | None = None
) -> list[str | None]:
    """Resolve exp_patch sentinels: "auto" -> detect from branch, "none" -> None.

    Raises ValueError if "auto" is used but no CL is found on the current branch.
    """
    return [_resolve_patch_sentinel(p, cwd=cwd) for p in exp_patches]


def create_pinpoint_jobs(
    benchmarks: list[str],
    configurations: list[str],
    *,
    story: str | None = None,
    story_tags: str | None = None,
    base_git_hash: str | None = None,
    exp_git_hash: str | None = None,
    base_patch: str | None = None,
    exp_patches: list[str | None],
    base_js_flags: str | None = None,
    exp_js_flags_list: list[str | None] | None = None,
    repeat: int = 150,
    bug_id: int | None = None,
    on_auto_hash: callable = None,
    on_job_created: callable = None,
    on_watching: callable = None,
    watch: bool | None = None,
) -> list[dict]:
    """Shared core for creating Pinpoint A/B jobs.

    Creates one job per combination of configuration x benchmark x exp_patch x exp_js_flags.

    exp_patches: list of resolved patch URLs or None entries.  Callers should
    use resolve_exp_patches() first to handle "auto"/"none" sentinels.

    Callbacks (optional, used by CLI for terminal output):
      on_auto_hash(cfg, commit, build_num):  called when a git hash is auto-detected
      on_job_created(index, total, combo, job): called after each job is created
      on_watching(url):  called for each job URL being watched

    watch:  True = always watch, None = auto (when chat is configured), False = never

    Returns a list of job detail dicts.
    """
    import itertools

    from . import daemon

    # Resolve benchmark aliases to (benchmark, story) pairs
    pairs = []
    for b in benchmarks:
        if b in pinpoint.BENCHMARK_ALIASES:
            pairs.append(pinpoint.BENCHMARK_ALIASES[b])
        else:
            pairs.append((b, story))

    if exp_js_flags_list is None:
        exp_js_flags_list = [None]

    # Auto-detect latest cached CI build when no git hash is specified
    auto_hashes: dict[str, str] = {}
    if base_git_hash is None and exp_git_hash is None:
        for cfg in configurations:
            try:
                commit, build_num = pinpoint.fetch_latest_build_commit(cfg)
                auto_hashes[cfg] = commit
                if on_auto_hash:
                    on_auto_hash(cfg, commit, build_num)
            except Exception as e:
                if on_auto_hash:
                    on_auto_hash(cfg, None, e)

    # Pre-fetch Gerrit subjects for human-readable job names
    patch_subjects: dict[str, str | None] = {}
    for p in exp_patches:
        if p and p not in patch_subjects:
            patch_subjects[p] = pinpoint.fetch_gerrit_subject(p)
    if base_patch and base_patch not in patch_subjects:
        patch_subjects[base_patch] = pinpoint.fetch_gerrit_subject(base_patch)

    combos = list(
        itertools.product(configurations, pairs, exp_patches, exp_js_flags_list)
    )
    jobs = []
    for i, (cfg, (bench, default_story), exp_patch, exp_js_flags) in enumerate(combos):
        git_hash = auto_hashes.get(cfg)
        # Build human-readable job name
        subject = patch_subjects.get(exp_patch) if exp_patch else None
        parts = []
        if subject:
            parts.append(subject)
        elif exp_js_flags:
            parts.append(f"flags: {exp_js_flags}")
        parts.append(f"({cfg}, {bench})")
        job_name = " ".join(parts)

        result = pinpoint.create_job(
            benchmark=bench,
            configuration=cfg,
            story=story or default_story,
            story_tags=story_tags,
            base_git_hash=base_git_hash or git_hash or "HEAD",
            exp_git_hash=exp_git_hash or git_hash or "HEAD",
            base_patch=base_patch,
            exp_patch=exp_patch,
            base_js_flags=base_js_flags,
            exp_js_flags=exp_js_flags,
            repeat=repeat,
            bug_id=bug_id,
            name=job_name,
        )
        job_url = result.get("url")
        if job_url:
            job_detail = _fetch_job_detail(job_url)
            jobs.append(job_detail)
        else:
            jobs.append(result)
        if on_job_created:
            on_job_created(
                i,
                len(combos),
                (cfg, bench, default_story, exp_patch, exp_js_flags),
                jobs[-1],
            )

    # Watch jobs
    cfg_obj = config.load()
    should_watch = watch or (
        watch is None and (cfg_obj.chat_webhook or cfg_obj.chat_app_space)
    )
    if should_watch:
        urls = [
            j.get("url")
            or f"https://pinpoint-dot-chromeperf.appspot.com/job/{j['job_id']}"
            for j in jobs
            if j.get("job_id")
        ]
        if urls:
            if not daemon.is_running():
                daemon.start_background()
            for url in urls:
                daemon.send_job(url)
                chat_notify_watching(url)
                if on_watching:
                    on_watching(url)

    return jobs
