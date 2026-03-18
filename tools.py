"""MCP tool definitions for v8-utils."""

import concurrent.futures
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

import config
import daemon
import gerrit as gerrit_tools
import jsb as jsb_module
import perf as perf_tools
import pinpoint

mcp = FastMCP("v8-utils", log_level="WARNING")


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


def _text_result(text: str) -> CallToolResult:
    """Return a CallToolResult with both content and structuredContent.

    Setting structuredContent.content makes Claude Code display the text
    with proper newlines instead of a collapsed JSON blob (see
    anthropics/claude-code#9962).
    """
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
    )


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


@mcp.tool()
def pinpoint_show_job(job_url: str) -> CallToolResult:
    """Fetch and display key information about one or more Pinpoint jobs.

    job_url: space-separated Pinpoint job URL(s) or job ID(s), e.g.
             https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000
    """
    urls = job_url.split()
    if not urls:
        return _text_result("No job URLs provided.")

    def fetch(u: str) -> str:
        try:
            return _format_job_detail(_fetch_job_detail(u))
        except Exception as e:
            return f"Error fetching {u}: {e}"

    fns = [lambda u=u: fetch(u) for u in urls]
    details = _run_concurrent(fns)
    return _text_result("\n\n".join(details))


@mcp.tool()
def pinpoint_cancel_job(
    job_urls: str,
    reason: str = "Cancelled",
) -> CallToolResult:
    """Cancel one or more Pinpoint jobs. Requires luci-auth login.

    job_urls: space-separated Pinpoint job URL(s) or job ID(s)
    reason:   cancellation reason (default: "Cancelled")
    """
    urls = job_urls.split()
    if not urls:
        return _text_result("No job URLs provided.")

    def cancel(url: str) -> str:
        try:
            result = pinpoint.cancel_job(url, reason)
            job_id = result.get("job_id", pinpoint.job_id_from_url(url))
            state = result.get("state", "unknown")
            return f"Job {job_id}: {state}"
        except Exception as e:
            job_id = pinpoint.job_id_from_url(url)
            return f"Job {job_id}: Error: {e}"

    fns = [lambda u=u: cancel(u) for u in urls]
    results = _run_concurrent(fns)
    return _text_result("\n".join(results))


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


@mcp.tool()
def pinpoint_list_jobs(
    count: int = 20,
    user: str | None = None,
    patch: str | None = None,
    status: str | None = None,
    benchmark: str | None = None,
    bot: str | None = None,
    since: str = "one month ago",
) -> CallToolResult:
    """List recent Pinpoint jobs for a user, newest first. CQ jobs are excluded.

    Requires luci-auth login when user is not specified:
      luci-auth login -scopes https://www.googleapis.com/auth/userinfo.email

    count:     number of jobs to return (default: 20)
    user:      user email (default: current luci-auth user)
    patch:     filter by Gerrit CL — any URL form, change ID, or crrev.
               "auto" detects from current branch; "none" clears the filter.
    status:    filter by status: Completed, Running, Failed, Cancelled, Queued
    benchmark: filter by benchmark name or alias:
                 "js3" → jetstream-main.crossbench
                 "js2" → jetstream2.crossbench
                 "sp3" → speedometer3.crossbench
    bot:       filter by bot configuration name or alias:
                 "linux" → linux-r350-perf
                 "m1"    → mac-m1_mini_2020-perf
                 "m2"    → mac-m2-pro-perf
                 "m3"    → mac-m3-pro-perf
                 "m4"    → mac-m4-mini-perf
    since:     only show jobs created after this date (default: "one month ago").
               Accepts natural language ("2 weeks ago", "yesterday") or ISO dates.
               Use "all" to disable the cutoff.

    All filters are ANDed together.
    """
    patch = resolve_patch_filter(patch)
    filters = []
    if patch:
        filters.append(f"patch={patch}")
    if status:
        filters.append(f"status={status}")
    if benchmark:
        filters.append(f"benchmark={benchmark}")
    if bot:
        filters.append(f"bot={bot}")
    since_dt = pinpoint.parse_since(since)
    jobs = _fetch_jobs_list(count, user, filters or None, since=since_dt)
    if not jobs:
        return _text_result("No jobs found.")
    return _text_result(_format_job_list(jobs))


def _format_job_list(jobs: list[dict]) -> str:
    """Format job list as compact text (mirrors pp's list-jobs output)."""
    import concurrent.futures

    patches = [j.get("experiment_patch") or "" for j in jobs]
    with concurrent.futures.ThreadPoolExecutor() as ex:
        subjects = list(
            ex.map(
                lambda p: pinpoint.fetch_gerrit_subject(p) if p else None,
                patches,
            )
        )

    blocks = []
    for j, subject in zip(jobs, subjects):
        created = (j.get("created") or "")[:16].replace("T", " ")
        status = j.get("status") or "?"
        url = j.get("url") or ""
        cfg = pinpoint.short_configuration(j.get("configuration") or "")
        benchmark = pinpoint.short_benchmark(j.get("benchmark") or "")
        story = j.get("story") or ""
        diff = j.get("difference_count")
        patch = j.get("experiment_patch") or ""
        base_flags = j.get("base_extra_args") or ""
        exp_flags = j.get("experiment_extra_args") or ""

        label = f"{benchmark} / {story}".strip(" /")
        diff_str = f"  diffs={diff}" if diff is not None else ""
        lines = [f"{created}  {status:<12}  {url}"]
        lines.append(f"  {cfg}  {label}{diff_str}")
        if patch:
            subject_str = f'  "{subject}"' if subject else ""
            lines.append(f"  patch:      {patch}{subject_str}")
        if base_flags:
            lines.append(f"  base-flags: {base_flags}")
        if exp_flags:
            lines.append(f"  exp-flags:  {exp_flags}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _results_header(job_id: str) -> str:
    """Build the header lines (bot/benchmark/patch/flags) for a results table."""
    try:
        job = _fetch_job_detail(job_id)
    except Exception:
        job = {}
    patch_url = job.get("experiment_patch")
    patch_subject = pinpoint.fetch_gerrit_subject(patch_url) if patch_url else None
    base_flags = job.get("base_extra_args")
    exp_flags = job.get("experiment_extra_args")

    lines: list[str] = []
    header_parts = []
    configuration = job.get("configuration")
    if configuration:
        header_parts.append(f"bot: {pinpoint.short_configuration(configuration)}")
    benchmark = job.get("benchmark")
    story = job.get("story")
    if benchmark:
        bench_str = f"benchmark: {pinpoint.short_benchmark(benchmark)}"
        if story:
            bench_str += f" / {story}"
        header_parts.append(bench_str)
    if header_parts:
        lines.append("  ".join(header_parts))
    if patch_url:
        patch_line = f"patch: {patch_url}"
        if patch_subject:
            patch_line += f'  "{patch_subject}"'
        lines.append(patch_line)
    if base_flags:
        lines.append(f"base-flags: {base_flags}")
    if exp_flags:
        lines.append(f"exp-flags:  {exp_flags}")
    return "\n".join(lines)


def _format_results_table(job_id: str, show_all: bool, use_cas: bool) -> str | None:
    """Format a results table for a single job. Returns None if no results.

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
        return f"Error: {e}"
    if not all_rows:
        return None

    rows = all_rows if show_all else [r for r in all_rows if r["significant"]]
    omitted = len(all_rows) - len(rows)
    if not rows:
        header = _results_header(job_id)
        if header:
            return f"{header}\n(no statistically significant results)"
        return "(no statistically significant results)"

    def pct(r: dict) -> float:
        bm = r["base_mean"] or 0
        return (r["exp_mean"] - bm) / bm * 100 if bm else 0

    rows.sort(key=pct, reverse=True)

    def _direction(unit: str | None) -> str:
        """Return a direction indicator for the unit."""
        if unit and "biggerIsBetter" in unit:
            return "bigger-better"
        if unit and "smallerIsBetter" in unit:
            return "smaller-better"
        return ""

    cells = []
    for r in rows:
        bm, bs = r["base_mean"] or 0, r["base_stdev"] or 0
        em, es = r["exp_mean"] or 0, r["exp_stdev"] or 0
        cells.append(
            (
                r["name"],
                f"{bm:.3f} ±{bs:.3f}",
                f"{em:.3f} ±{es:.3f}",
                f"{pct(r):+.2f}%",
                f"{r['p_value']:.4f}",
                "*" if r["significant"] else "",
                _direction(r.get("unit")),
            )
        )

    hdrs = (
        "metric",
        "base mean±stdev",
        "exp mean±stdev",
        "chg%",
        "p",
        "sig",
        "direction",
    )
    widths = [max(len(h), max(len(c[i]) for c in cells)) for i, h in enumerate(hdrs)]

    def fmt_row(cols: tuple) -> str:
        return "  ".join(
            c.ljust(widths[i]) if i == 0 else c.rjust(widths[i])
            for i, c in enumerate(cols)
        )

    sep = "-" * (sum(widths) + 2 * (len(widths) - 1))
    header = _results_header(job_id)
    lines: list[str] = [header] if header else []
    lines += [
        "",
        fmt_row(hdrs),
        sep,
        *[fmt_row(c) for c in cells],
    ]
    if omitted:
        lines.append(
            f"({omitted} non-significant result{'s' if omitted != 1 else ''} omitted)"
        )
    return "\n".join(lines)


@mcp.tool()
def pinpoint_show_results(
    job_url: str = "",
    show_all: bool = False,
    use_cas: bool = False,
    recent: int | None = None,
    patch: str | None = None,
    status: str | None = None,
    benchmark: str | None = None,
    bot: str | None = None,
    since: str | None = None,
) -> CallToolResult:
    """Show a base-vs-experiment comparison table for a Pinpoint job.

    One row per metric: base mean±stdev, exp mean±stdev, %change, p-value,
    significance marker. Sorted by %change descending.

    job_url:   space-separated Pinpoint job URL(s) or job ID(s)
    show_all:  if False (default), only show statistically significant results.
    use_cas:   if True, fetch raw per-run values from CAS isolates instead of
               the histogram HTML. Slower but surfaces richer sub-metrics for
               JetStream (Score, First, Average, Worst4 per story).
               Requires: gcloud auth application-default login
    recent:    if set, show results for the N most recent completed jobs
               for the current user. Can be combined with job_url.
    patch:     filter by Gerrit CL — any URL form, change ID, or crrev.
               "auto" detects from current branch; "none" clears the filter.
    status:    filter by status (in addition to the default Completed filter)
    benchmark: filter by benchmark name or alias (js3, js2, sp3)
    bot:       filter by bot configuration name or alias (m1, m2, m3, m4, linux)
    since:     only include jobs after this date (default: "one month ago" when
               filters are used). Accepts natural language or ISO dates.
               Use "all" for no limit.
    """
    patch = resolve_patch_filter(patch)
    job_ids: list[str] = []
    if job_url:
        job_ids.extend(pinpoint.job_id_from_url(u) for u in job_url.split())

    filters = ["status=Completed"]
    if patch:
        filters.append(f"patch={patch}")
    if status:
        filters.append(f"status={status}")
    if benchmark:
        filters.append(f"benchmark={benchmark}")
    if bot:
        filters.append(f"bot={bot}")
    has_filters = len(filters) > 1 or recent

    if recent or has_filters:
        since_str = since or ("one month ago" if has_filters else None)
        since_dt = pinpoint.parse_since(since_str) if since_str else None
        count = recent or 20
        jobs = _fetch_jobs_list(count=count, filters=filters, since=since_dt)
        job_ids.extend(j["job_id"] for j in jobs)

    if not job_ids:
        return _text_result(
            "Provide a job_url, use recent=N, or pass filter flags (patch, benchmark, bot)."
        )

    fns = [
        lambda jid=jid: _format_results_table(jid, show_all, use_cas) for jid in job_ids
    ]
    tables = _run_concurrent(fns)

    multi = len(job_ids) > 1
    blocks = []
    for job_id, table in zip(job_ids, tables):
        header = f"── https://pinpoint-dot-chromeperf.appspot.com/job/{job_id}"
        if table is None:
            blocks.append(f"{header}\nNo results found.")
        else:
            blocks.append(f"{header}\n{table}" if multi else table)
    return _text_result("\n\n".join(blocks))


def get_gerrit_issue_url() -> str | None:
    """Read the Gerrit CL URL for the current git branch from git config.

    Returns a full URL including patchset, e.g.:
      https://chromium-review.googlesource.com/7650974/1
    Returns None if not inside a git repo or the branch has no associated CL.
    """
    import subprocess as _sp

    def _git(*args: str) -> str:
        r = _sp.run(["git"] + list(args), capture_output=True, text=True)
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
            import chat

            chat.notify(
                cfg.chat_app_space,
                cfg.chat_service_account_email,
                f"👀 Watching: {job_url}",
            )
        except Exception:
            pass
    elif cfg.chat_webhook:
        try:
            import httpx

            httpx.post(
                cfg.chat_webhook, json={"text": f"👀 Watching: {job_url}"}, timeout=10
            )
        except Exception:
            pass


def _job_url(job: dict) -> str | None:
    """Extract the Pinpoint job URL from a job detail dict."""
    jid = job.get("job_id")
    if not jid:
        return None
    return job.get("url") or f"https://pinpoint-dot-chromeperf.appspot.com/job/{jid}"


def _resolve_patch_sentinel(value: str) -> str | None:
    """Resolve a single patch sentinel: "auto" → detect from branch, "none" → None.

    Returns the resolved URL string, None (for "none"), or the original value.
    Raises ValueError if "auto" is used but no CL is found on the current branch.
    """
    if value.lower() == "none":
        return None
    if value.lower() == "auto":
        detected = get_gerrit_issue_url()
        if detected is None:
            import subprocess as _sp

            branch = (
                _sp.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True,
                    text=True,
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


def resolve_exp_patches(exp_patches: list[str]) -> list[str | None]:
    """Resolve exp_patch sentinels: "auto" → detect from branch, "none" → None.

    Raises ValueError if "auto" is used but no CL is found on the current branch.
    """
    return [_resolve_patch_sentinel(p) for p in exp_patches]


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

    Creates one job per combination of configuration × benchmark × exp_patch × exp_js_flags.

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

    combos = list(
        itertools.product(configurations, pairs, exp_patches, exp_js_flags_list)
    )
    jobs = []
    for i, (cfg, (bench, default_story), exp_patch, exp_js_flags) in enumerate(combos):
        git_hash = auto_hashes.get(cfg)
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
        urls = [u for j in jobs if (u := _job_url(j))]
        if urls:
            if not daemon.is_running():
                daemon.start_background()
            for url in urls:
                daemon.send_job(url)
                chat_notify_watching(url)
                if on_watching:
                    on_watching(url)

    return jobs


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


@mcp.tool()
def pinpoint_create_job(
    benchmark: str = "js3 sp3",
    configuration: str = "m1",
    exp_patch: str = "auto",
    story: str | None = None,
    story_tags: str | None = None,
    base_git_hash: str | None = None,
    exp_git_hash: str | None = None,
    base_patch: str | None = None,
    base_js_flags: str | None = None,
    exp_js_flags: str | None = None,
    repeat: int = 150,
    bug_id: int | None = None,
) -> CallToolResult:
    """Create Pinpoint A/B try jobs. Requires luci-auth login.

    Creates one job per combination of benchmark × configuration.
    Pass multiple space-separated values to create jobs in bulk, e.g.:
      benchmark="js3 sp3" configuration="m1 m4"  →  4 jobs

    benchmark:      space-separated benchmark names or aliases (default: "js3 sp3"):
                      "js3"  → jetstream-main.crossbench (story: JetStream)
                      "js2"  → jetstream2.crossbench     (story: JetStream2)
                      "sp3"  → speedometer3.crossbench   (story: Speedometer3)
    configuration:  space-separated bot config(s) or alias(es) (default: "m1"):
                      "linux" → linux-r350-perf
                      "m1"    → mac-m1_mini_2020-perf
                      "m2"    → mac-m2-pro-perf
                      "m3"    → mac-m3-pro-perf
                      "m4"    → mac-m4-mini-perf
    exp_patch:      REQUIRED — experiment patch. One of:
                      "auto"  → auto-detect from the current git branch's Gerrit CL
                                (fails if no CL is found)
                      "none"  → no patch (for flag-only or hash-only comparisons)
                      "<url>" → explicit Gerrit CL URL, change ID, or crrev/c/N
    story:          story within the benchmark (overrides alias default)
    story_tags:     comma-separated story tags to select stories
    base_git_hash:  git hash for the base build (default: auto-detected latest CI build)
    exp_git_hash:   git hash for the experiment build (default: auto-detected latest CI build)
    base_patch:     Gerrit patch for base — change ID, crrev/c/12345, or full URL
    base_js_flags:  V8 flags for base, passed as --js-flags="...", e.g. "--turbofan"
    exp_js_flags:   V8 flags for experiment, same format
    repeat:         number of bot runs per variant (default: 150)
    bug_id:         buganizer issue ID to associate with the job
    """
    jobs = create_pinpoint_jobs(
        benchmarks=benchmark.split(),
        configurations=configuration.split(),
        story=story,
        story_tags=story_tags,
        base_git_hash=base_git_hash,
        exp_git_hash=exp_git_hash,
        base_patch=base_patch,
        exp_patches=resolve_exp_patches([exp_patch]),
        base_js_flags=base_js_flags,
        exp_js_flags_list=[exp_js_flags],
        repeat=repeat,
        bug_id=bug_id,
    )
    return _text_result("\n\n".join(_format_job_detail(j) for j in jobs))


# ── d8 ───────────────────────────────────────────────────────────────────────

_MAX_D8_OUTPUT = 5_000


@mcp.tool()
def run_d8(
    args: list[str],
    build: str | None = None,
    cwd: str | None = None,
    timeout: int = 60,
    stdout_file: str | None = None,
    stderr_file: str | None = None,
) -> CallToolResult:
    """Run the d8 JavaScript shell with the given arguments.

    For benchmarking, use the jsb_run_bench tool instead.

    args:        arguments to pass to d8 (e.g. ["--prof", "script.js"])
    build:       build directory name under v8_out (default: config default_build)
    cwd:         working directory for d8 (default: v8_out parent)
    timeout:     max seconds before killing the process (default: 60)
    stdout_file: redirect stdout to this file path instead of capturing
    stderr_file: redirect stderr to this file path instead of capturing
    """
    import subprocess

    cfg = config.load()
    build = build or cfg.default_build
    d8 = cfg.v8_out / build / "d8"
    if not d8.exists():
        raise ValueError(f"d8 not found: {d8}")

    cmd = [str(d8), *args]
    stdout = open(stdout_file, "w") if stdout_file else subprocess.PIPE
    stderr = open(stderr_file, "w") if stderr_file else subprocess.PIPE
    try:
        result = subprocess.run(
            cmd,
            stdout=stdout,
            stderr=stderr,
            text=True,
            timeout=timeout,
            errors="replace",
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return _text_result(f"Error: d8 timed out after {timeout}s")
    except Exception as e:
        return _text_result(f"Error: {e}")
    finally:
        if stdout_file:
            stdout.close()
        if stderr_file:
            stderr.close()

    parts: list[str] = []
    if stdout_file:
        parts.append(f"[stdout → {stdout_file}]")
    elif result.stdout:
        parts.append(result.stdout)
    if stderr_file:
        parts.append(f"[stderr → {stderr_file}]")
    elif result.stderr:
        parts.append("[stderr]\n" + result.stderr)
    if result.returncode not in (0, 1):
        parts.append(f"[exit {result.returncode}]")

    out = "\n".join(parts).strip()
    if not out:
        out = "(no output)"
    if len(out) > _MAX_D8_OUTPUT:
        out = (
            out[:_MAX_D8_OUTPUT]
            + f"\n\n[truncated — {len(out) - _MAX_D8_OUTPUT:,} more chars. "
            "Use stdout_file/stderr_file to redirect large output to a file.]"
        )
    return _text_result(out)


# ── jsb tools ────────────────────────────────────────────────────────────────


@mcp.tool()
def jsb_run_bench(
    bench: str,
    builds: list[str],
    runs: int = 5,
    suite: str = "js3",
    perf: bool = False,
    perf_upload: bool = False,
) -> CallToolResult:
    """Run a JetStream2/3 story with one or more JS shell builds and return scores.

    bench:  benchmark story name, e.g. "regexp-octane", "chai-wtb"
    builds: list of "build[:flags]" specs under v8_out, or full paths
            to any JS shell binary (d8, jsc, etc.), e.g.:
              ["release-main", "release-lto:--turbolev-future",
               "/home/user/v8-alt/out/release/d8",
               "/home/user/WebKit/WebKitBuild/Release/bin/jsc"]
    runs:   number of runs per variant (default: 5)
    suite:  "js2" or "js3" (default: "js3")
    perf:   if True, record a local perf trace (no upload) instead of
            running for scores.  Requires exactly one build.  Returns the
            perf.data path for use with perf_hotspots, perf_annotate, etc.
    perf_upload: like perf, but also uploads the trace via pprof.

    Returns a formatted comparison table with mean, stdev, and delta
    (with significance) when two variants are given.
    """
    cfg = config.load()
    js3 = suite.lower() != "js2"
    suite_dir = cfg.js3_dir if js3 else cfg.js2_dir
    suite_label = "JS3" if js3 else "JS2"

    variants = [jsb_module.Variant.parse(b) for b in builds]
    for v in variants:
        d8 = v.d8(cfg.v8_out)
        if not d8.exists():
            raise ValueError(f"d8 not found: {d8}")

    if perf or perf_upload:
        if perf and perf_upload:
            raise ValueError("perf and perf_upload are mutually exclusive")
        if len(variants) != 1:
            raise ValueError("perf/perf_upload requires exactly one build")
        return _text_result(
            jsb_module.run_perf(
                variants[0],
                suite_dir,
                bench,
                cfg.v8_out,
                cfg.perf_script,
                upload=perf_upload,
            )
        )

    results = [
        jsb_module.run_variant(v, suite_dir, bench, runs, js3, cfg.v8_out)
        for v in variants
    ]

    return _text_result(
        jsb_module.format_table(bench, suite_label, runs, variants, results)
    )


# ── gerrit tools ─────────────────────────────────────────────────────────────


@mcp.tool()
def gerrit_comments(change_url: str) -> CallToolResult:
    """Fetch all published comments on a Gerrit CL, threaded by file and line.

    Each entry represents a comment thread and includes:
      file, line, patch_set, author, message, updated, replies[]

    Threads are sorted by file path then line number.  Use this to understand
    reviewer feedback or the current state of a code review.

    change_url: Gerrit CL URL, e.g.:
      https://chromium-review.googlesource.com/c/v8/v8/+/7650974
      https://chromium-review.googlesource.com/7650974
    """
    threads = gerrit_tools.comments(change_url)
    if not threads:
        return _text_result("No comments found.")
    return _text_result(_format_gerrit_comments(threads))


def _format_gerrit_comments(threads: list[dict]) -> str:
    blocks = []
    for t in threads:
        loc = t["file"]
        if t.get("line"):
            loc += f":{t['line']}"
        if t.get("patch_set"):
            loc += f" (ps{t['patch_set']})"
        status = " [unresolved]" if t.get("unresolved") else ""
        header = f"{loc}{status}"
        author = t.get("author", "unknown")
        msg = t.get("message", "").strip()
        lines = [header, f"  {author}: {msg}"]
        for r in t.get("replies", []):
            r_author = r.get("author", "unknown")
            r_msg = r.get("message", "").strip()
            lines.append(f"  {r_author}: {r_msg}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


@mcp.tool()
def gerrit_fetch(
    change_url: str,
    repo_path: str = ".",
    fetch: bool = True,
) -> dict:
    """Return the git ref for a Gerrit CL patchset, optionally fetching it.

    Gerrit stores each patchset at refs/changes/NN/CHANGE_ID/PATCHSET.
    If fetch=True (default), runs `git fetch` in repo_path.

    Returns: ref, remote, patchset, fetch_head (commit SHA, if fetched)

    The patchset is fetched but NOT checked out — the working tree is
    unchanged.  To read file contents or diffs, use git commands that
    reference the commit directly.

    After a successful fetch, use the returned `fetch_head` SHA — do NOT
    use FETCH_HEAD (it may have changed by the time you run the next command):

      git show <fetch_head>                    # view the patchset commit
      git show <fetch_head>:path/to/file.cc   # read a file as it is in the patch
      git diff <fetch_head>^..<fetch_head>     # diff introduced by the commit
      git log <fetch_head>                     # history up to the patchset

    If no patchset is in the URL, the latest patchset is fetched.

    change_url: Gerrit CL URL (with or without patchset suffix)
    repo_path:  local git repo to fetch into (default: current directory)
    fetch:      if False, return ref/remote without running git fetch
                (useful for getting the ref name to fetch manually)
    """
    return gerrit_tools.fetch_ref(change_url, repo_path=repo_path, fetch=fetch)


# ── repo tools ───────────────────────────────────────────────────────────────

_REPO_ALIASES: dict[str, str] = {
    "jsc": "jsc_dir",
    "js2": "js2_dir",
    "js3": "js3_dir",
    "spidermonkey": "spidermonkey_dir",
}

_MAX_READ_LINES = 2000
_MAX_GREP_MATCHES = 100


def _resolve_repo(repo: str) -> Path:
    """Resolve a repo alias to its configured path, or raise ValueError."""
    key = _REPO_ALIASES.get(repo)
    if key is None:
        valid = ", ".join(sorted(_REPO_ALIASES))
        raise ValueError(f"Unknown repo {repo!r}. Valid repos: {valid}")
    cfg = config.load()
    path = getattr(cfg, key)
    if path is None:
        raise ValueError(
            f"Repo {repo!r} is not configured. "
            f"Set {key} in ~/.config/v8-utils/config.toml, e.g.:\n"
            f'  {key} = "~/path/to/{repo}"'
        )
    if not path.is_dir():
        raise ValueError(f"Repo {repo!r} path does not exist: {path}")
    return path


@mcp.tool()
def repo_read(
    repo: str,
    path: str,
    offset: int = 0,
    limit: int = _MAX_READ_LINES,
    ref: str | None = None,
) -> CallToolResult:
    """Read a file from a related source repo.

    repo:   repo alias — one of: jsc, js2, js3, spidermonkey
    path:   file path relative to the repo root, e.g. "runtime/RegExp.cpp"
    offset: 0-based line offset to start reading from (default: 0)
    limit:  max lines to return (default: 2000)
    ref:    git ref to read from (e.g. commit hash, branch, tag).
            If omitted, reads from the working tree.
    """
    import subprocess

    root = _resolve_repo(repo)

    if ref:
        proc = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            capture_output=True,
            text=True,
            cwd=root,
        )
        if proc.returncode != 0:
            raise ValueError(
                f"git show {ref}:{path} failed: {proc.stderr.strip()[:500]}"
            )
        lines = proc.stdout.splitlines()
    else:
        target = (root / path).resolve()
        # Prevent path traversal outside repo root
        if not str(target).startswith(str(root)):
            raise ValueError(f"Path escapes repo root: {path}")
        if not target.is_file():
            raise ValueError(f"File not found: {path} (in {root})")
        lines = target.read_text(errors="replace").splitlines()
    total = len(lines)
    selected = lines[offset : offset + limit]
    result = "\n".join(f"{i + offset + 1:6}\t{line}" for i, line in enumerate(selected))
    if offset + limit < total:
        result += f"\n(truncated — showing lines {offset + 1}–{offset + len(selected)} of {total}; use offset/limit to paginate)"
    return _text_result(result)


@mcp.tool()
def repo_grep(
    repo: str,
    pattern: str,
    glob: str | None = None,
    context: int = 0,
    limit: int = _MAX_GREP_MATCHES,
    ref: str | None = None,
) -> CallToolResult:
    """Search for a pattern in a related source repo using git grep.

    repo:    repo alias — one of: jsc, js2, js3, spidermonkey
    pattern: regex pattern to search for
    glob:    optional file glob filter, e.g. "*.cpp" or "*.{h,cpp}"
    context: lines of context around each match (default: 0)
    limit:   max matches to return (default: 100)
    ref:     git ref to search in (e.g. commit hash, branch, tag).
             If omitted, searches the working tree.
    """
    import subprocess

    root = _resolve_repo(repo)
    cmd = ["git", "grep", "-n", "--no-color", "-E", pattern]
    if context > 0:
        cmd.extend([f"-C{context}"])
    if ref:
        cmd.append(ref)
    if glob:
        cmd.extend(["--", glob])

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=root,
    )
    collected: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            collected.append(line.rstrip("\n"))
            if len(collected) >= limit + 1:
                proc.kill()
                break
    finally:
        proc.wait()

    if not collected and proc.returncode == 1:
        return _text_result("No matches found.")
    if not collected and proc.returncode not in (0, 1, -9):
        stderr = proc.stderr.read() if proc.stderr else ""
        raise ValueError(f"git grep failed: {stderr.strip()[:500]}")

    if len(collected) > limit:
        result = "\n".join(collected[:limit])
        result += f"\n(truncated — showing first {limit} matches)"
    else:
        result = "\n".join(collected)
    return _text_result(result)


# ── perf tools ────────────────────────────────────────────────────────────────


@mcp.tool()
def perf_stat(stat_file: str) -> CallToolResult:
    """Parse a saved `perf stat` output file into structured counter data.

    stat_file: path to a file containing `perf stat` text output
               (saved via `perf stat -o <file>` or stderr redirection)

    Returns elapsed_seconds and a list of counters with their values and
    human-readable notes (e.g. "3.45 CPUs utilized").
    """
    data = perf_tools.parse_stat(stat_file)
    lines = []
    if data.get("elapsed_seconds") is not None:
        lines.append(f"elapsed: {data['elapsed_seconds']:.3f}s")
        lines.append("")
    for c in data.get("counters", []):
        val = f"{c['value']:>15,.0f}  {c['counter']}"
        if c.get("note"):
            val += f"  # {c['note']}"
        lines.append(val)
    return _text_result("\n".join(lines) if lines else "No counters found.")


@mcp.tool()
def perf_hotspots(
    perf_data: str,
    dso: str | None = None,
    n: int = 30,
) -> CallToolResult:
    """Return the top N hot symbols from a perf.data file.

    Each entry includes self_pct (exclusive time) and total_pct (inclusive
    time including callees), plus the symbol name and shared object.
    Sorted by self_pct descending — start here to find what to annotate.

    perf_data: path to perf.data file
    dso:       restrict to a specific shared object, e.g. "libv8.so" or "d8"
    n:         number of symbols to return (default 30)
    """
    rows = perf_tools.hotspots(perf_data, dso=dso, n=n)
    if not rows:
        return _text_result("No symbols found.")
    lines = [f"{'self%':>6}  {'total%':>6}  {'dso':<20}  symbol"]
    lines.append("-" * len(lines[0]))
    for r in rows:
        total = f"{r['total_pct']:.1f}" if r.get("total_pct") is not None else "—"
        lines.append(
            f"{r['self_pct']:5.1f}%  {total:>5}%  {r['dso']:<20}  {r['symbol']}"
        )
    return _text_result("\n".join(lines))


@mcp.tool()
def perf_callers(
    perf_data: str,
    symbol: str,
    n: int = 20,
) -> CallToolResult:
    """Show who calls a hot symbol and with what sample weight.

    Returns the call-graph section for the symbol from perf report in
    caller mode, so the tree reads upward (direct callers nearest, then
    their callers above).  Use this to understand whether hotness is
    self-time or propagated from a call site.

    perf_data: path to perf.data file
    symbol:    symbol name or unique substring, e.g. "Heap::AllocateRaw"
    n:         max lines of call-graph detail to return (default 20)
    """
    return _text_result(perf_tools.callers(perf_data, symbol, n=n))


@mcp.tool()
def perf_annotate(
    perf_data: str,
    symbol: str,
    dso: str | None = None,
    min_pct: float = 0.5,
    context: int = 8,
) -> CallToolResult:
    """Annotated disassembly for a symbol, with smart hot-region extraction.

    Shows the 20 hottest instructions and contiguous hot code blocks
    (>= min_pct), each expanded by ±context lines and sorted by peak heat.

    Line numbers are included so you can call perf_annotate_read_around
    to explore surrounding code.

    perf_data: path to perf.data file
    symbol:    exact symbol name (use perf_hotspots to find it)
    dso:       shared object filter, e.g. "libv8.so"
    min_pct:   minimum sample % to qualify as hot (default 0.5)
    context:   lines of context around each hot cluster (default 8)
    """
    data = perf_tools.annotate(
        perf_data, symbol, dso=dso, min_pct=min_pct, context=context
    )
    lines = [
        f"{data['symbol']}  ({data['total_lines']} lines, min_pct={data['min_pct_threshold']}%)"
    ]
    if data.get("parse_warnings"):
        for w in data["parse_warnings"]:
            lines.append(f"warning: {w}")
    # Top instructions
    lines.append("")
    lines.append("Top instructions:")
    lines.append(f"{'line':>6}  {'pct':>6}  {'addr':<14}  asm")
    lines.append("-" * 60)
    for instr in data.get("top_instructions", []):
        lines.append(
            f"{instr['lineno']:6}  {instr['pct']:5.1f}%  {instr['addr']:<14}  {instr['asm']}"
        )
    # Hot blocks
    for i, block in enumerate(data.get("hot_blocks", [])):
        lines.append("")
        lines.append(
            f"Hot block #{i + 1} (lines {block['line_range']}, peak {block['peak_pct']:.1f}%):"
        )
        lines.append(block["content"])
    return _text_result("\n".join(lines))


@mcp.tool()
def perf_annotate_read_around(
    perf_data: str,
    symbol: str,
    line: int,
    context: int = 30,
    dso: str | None = None,
) -> CallToolResult:
    """Read a window of annotated disassembly around a specific line number.

    Use this after perf_annotate to explore regions of interest.  Line
    numbers are as reported in perf_annotate's top_instructions and
    hot_blocks fields.  Each output line is prefixed with its line number
    for further navigation.

    perf_data: path to perf.data file
    symbol:    symbol name (must match perf_annotate call)
    line:      1-based line number to centre the window on
    context:   lines before and after to include (default 30)
    dso:       shared object filter (must match perf_annotate call if used)
    """
    return _text_result(
        perf_tools.annotate_read_around(
            perf_data, symbol, line, context=context, dso=dso
        )
    )


@mcp.tool()
def perf_flamegraph(
    perf_data: str,
    focus_symbol: str | None = None,
    dso: str | None = None,
    min_pct: float = 0.5,
    depth: int = 8,
) -> CallToolResult:
    """Aggregated text flamegraph: all hot call paths in one view.

    Shows root→leaf call chains sorted by absolute sample percentage, so
    the dominant execution paths are immediately visible without iterative
    perf_callers traversal.

    Typical workflow:
      1. perf_hotspots  — find the hottest symbols
      2. perf_flamegraph(focus_symbol=X)  — understand full call context
      3. perf_annotate  — drill into hot instructions

    focus_symbol: restrict to call trees whose root matches this substring,
                  e.g. "RegExpPrototypeExec" or "Heap::AllocateRaw"
    dso:          restrict to a specific shared object, e.g. "libv8.so"
    min_pct:      omit paths below this % of total samples (default 0.5)
    depth:        maximum call-chain depth to expand (default 8)
    """
    return _text_result(
        perf_tools.flamegraph(
            perf_data, focus_symbol=focus_symbol, dso=dso, min_pct=min_pct, depth=depth
        )
    )


@mcp.tool()
def perf_tma(
    perf_data: str,
    symbol: str | None = None,
    n: int = 20,
) -> CallToolResult:
    """Microarchitecture bottleneck analysis (TMA Level 1) per symbol.

    Always safe to call — returns a message when the perf.data was not
    recorded with TMA events.

    Intensity fields = event_pct / cycles_pct for each symbol:
      ~1.0  proportional to cycle share (average)
      >1.0  disproportionately high — likely bottleneck
      <1.0  below average

    To enable: re-record with linux-perf-d8.py --topdown
    (Intel Skylake-SP; requires topdown-* kernel PMU events)

    Recommended workflow:
      1. perf_hotspots       — rank hot symbols
      2. perf_tma            — characterise bottleneck (works or tells you how)
      3. perf_flamegraph     — understand call context
      4. perf_annotate       — inspect hot instructions

    symbol:  filter to symbols containing this substring
    n:       max symbols to return, sorted by cycles_pct (default 20)
    """
    data = perf_tools.tma(perf_data, symbol=symbol, n=n)
    if not data.get("available"):
        return _text_result(data.get("message", "TMA data not available."))

    has_mem = data.get("has_mem_detail", False)
    hdr = f"{'cyc%':>6}  {'FE':>5}  {'Ret':>5}  {'Bad':>5}"
    if has_mem:
        hdr += f"  {'Mem':>5}"
    hdr += f"  {'dominant':<24}  symbol"
    lines = [hdr, "-" * len(hdr)]
    for s in data.get("symbols", []):
        row = (
            f"{s['cycles_pct']:5.1f}%"
            f"  {s['fe_intensity']:5.2f}"
            f"  {s['retiring_intensity']:5.2f}"
            f"  {s['bad_spec_intensity']:5.2f}"
        )
        if has_mem:
            mem = s.get("mem_intensity")
            row += f"  {mem:5.2f}" if mem is not None else "      —"
        row += f"  {s['dominant']:<24}  {s['symbol']}"
        lines.append(row)
    return _text_result("\n".join(lines))


@mcp.tool()
def perf_diff(
    perf_before: str,
    perf_after: str,
    dso: str | None = None,
    n: int = 30,
) -> CallToolResult:
    """Compare two perf profiles: what got hotter or cooler?

    Returns the top N symbols sorted by |delta_pct|, so the biggest
    changes appear first regardless of direction.

    perf_before: path to baseline perf.data
    perf_after:  path to experiment perf.data
    dso:         restrict to a specific shared object
    n:           number of symbols to return (default 30)
    """
    rows = perf_tools.diff(perf_before, perf_after, dso=dso, n=n)
    if not rows:
        return _text_result("No symbol differences found.")
    lines = [f"{'delta':>8}  {'base%':>6}  {'after%':>7}  {'dso':<20}  symbol"]
    lines.append("-" * len(lines[0]))
    for r in rows:
        base = (
            f"{r['baseline_pct']:.1f}%" if r.get("baseline_pct") is not None else "new"
        )
        after = f"{r['after_pct']:.1f}%" if r.get("after_pct") is not None else "gone"
        delta = r.get("delta_pct")
        delta_s = f"{delta:+.1f}%" if delta is not None else "—"
        lines.append(
            f"{delta_s:>8}  {base:>6}  {after:>7}  {r['dso']:<20}  {r['symbol']}"
        )
    return _text_result("\n".join(lines))
