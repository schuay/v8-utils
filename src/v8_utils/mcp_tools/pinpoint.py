"""MCP tools for Chromium Pinpoint A/B jobs."""

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from .. import pinpoint as pinpoint_mod
from ..tools import (
    _fetch_job_details_sorted,
    _fetch_jobs_list,
    _format_job_detail,
    _format_results_table,
    _run_concurrent,
    create_pinpoint_jobs,
    resolve_exp_patches,
    resolve_patch_filter,
)
from ._shared import _resolve_repo, _text_result


def _format_job_list(jobs: list[dict]) -> str:
    """Format job list as compact text (mirrors pp's list-jobs output)."""
    import concurrent.futures

    patches = [j.get("experiment_patch") or "" for j in jobs]
    with concurrent.futures.ThreadPoolExecutor() as ex:
        subjects = list(
            ex.map(
                lambda p: pinpoint_mod.fetch_gerrit_subject(p) if p else None,
                patches,
            )
        )

    blocks = []
    for j, subject in zip(jobs, subjects):
        created = (j.get("created") or "")[:16].replace("T", " ")
        status = j.get("status") or "?"
        url = j.get("url") or ""
        cfg = pinpoint_mod.short_configuration(j.get("configuration") or "")
        benchmark = pinpoint_mod.short_benchmark(j.get("benchmark") or "")
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


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def pinpoint_show_job(job_urls: str) -> CallToolResult:
        """Fetch and display key information about one or more Pinpoint jobs.

        job_urls: one or more Pinpoint job URLs or IDs, space-separated
                  (e.g. "14cc0d73090000 12fd3dd7090000")
        """
        urls = job_urls.split()
        if not urls:
            return _text_result("No job URLs provided.")

        paired = _fetch_job_details_sorted(urls)
        blocks = []
        for jid, detail in paired:
            if "error" in detail:
                blocks.append(f"Error fetching {jid}: {detail['error']}")
            else:
                blocks.append(_format_job_detail(detail))
        return _text_result("\n\n".join(blocks))

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
                result = pinpoint_mod.cancel_job(url, reason)
                job_id = result.get("job_id", pinpoint_mod.job_id_from_url(url))
                state = result.get("state", "unknown")
                return f"Job {job_id}: {state}"
            except Exception as e:
                job_id = pinpoint_mod.job_id_from_url(url)
                return f"Job {job_id}: Error: {e}"

        fns = [lambda u=u: cancel(u) for u in urls]
        results = _run_concurrent(fns)
        return _text_result("\n".join(results))

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
        since_dt = pinpoint_mod.parse_since(since)
        jobs = _fetch_jobs_list(count, user, filters or None, since=since_dt)
        if not jobs:
            return _text_result("No jobs found.")
        # Display oldest first (API returns newest first).
        jobs.reverse()
        return _text_result(_format_job_list(jobs))

    @mcp.tool()
    def pinpoint_show_results(
        job_urls: str = "",
        use_cas: bool = False,
        recent: int | None = None,
        patch: str | None = None,
        status: str | None = None,
        benchmark: str | None = None,
        bot: str | None = None,
        since: str | None = None,
    ) -> CallToolResult:
        """Show a base-vs-experiment comparison table for a Pinpoint job.

        One row per metric: base mean±stdev, exp mean±stdev, %change, p-value
        (Mann-Whitney U, α=0.01), direction (↑improved/↓regressed).
        Sorted by %change descending.

        job_urls:  one or more Pinpoint job URLs or IDs, space-separated
                   (e.g. "14cc0d73090000 12fd3dd7090000")
        use_cas:   if True, fetch raw per-run values from CAS isolates instead of
                   the histogram HTML. Slower but surfaces richer sub-metrics for
                   JetStream (Score, First, Average, Worst4 per story).
                   Requires: gcloud auth application-default login
        recent:    if set, show results for the N most recent completed jobs
                   for the current user. Can be combined with job_urls.
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
        if job_urls:
            job_ids.extend(pinpoint_mod.job_id_from_url(u) for u in job_urls.split())

        filters = ["status=Completed"]
        if patch:
            filters.append(f"patch={patch}")
        if status:
            filters.append(f"status={status}")
        if benchmark:
            filters.append(f"benchmark={benchmark}")
        if bot:
            filters.append(f"bot={bot}")
        has_filters = len(filters) > 1 or recent or since

        if recent or has_filters:
            since_str = since or ("one month ago" if has_filters else None)
            since_dt = pinpoint_mod.parse_since(since_str) if since_str else None
            count = recent or 20
            jobs = _fetch_jobs_list(count=count, filters=filters, since=since_dt)
            job_ids.extend(j["job_id"] for j in jobs)

        if not job_ids:
            return _text_result(
                "Provide job_urls, use recent=N, or pass filter flags (patch, benchmark, bot)."
            )

        paired = _fetch_job_details_sorted(job_ids)
        job_ids = [jid for jid, _ in paired]
        detail_map = dict(paired)

        fns = [
            lambda jid=jid: _format_results_table(
                jid, False, use_cas, job=detail_map.get(jid)
            )
            for jid in job_ids
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
        v8_repo_path: str | None = None,
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
        v8_repo_path:   absolute path to the v8 repo for "auto" patch detection
                        (default: configured v8 repo).
                        Must point to the correct worktree when using worktrees.
        """
        repo_path = v8_repo_path or str(_resolve_repo("v8"))
        jobs = create_pinpoint_jobs(
            benchmarks=benchmark.split(),
            configurations=configuration.split(),
            story=story,
            story_tags=story_tags,
            base_git_hash=base_git_hash,
            exp_git_hash=exp_git_hash,
            base_patch=base_patch,
            exp_patches=resolve_exp_patches([exp_patch], cwd=repo_path),
            base_js_flags=base_js_flags,
            exp_js_flags_list=[exp_js_flags],
            repeat=repeat,
            bug_id=bug_id,
        )
        return _text_result("\n\n".join(_format_job_detail(j) for j in jobs))
