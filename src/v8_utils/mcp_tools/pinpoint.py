"""MCP tools for Chromium Pinpoint A/B jobs."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from .. import pinpoint as pinpoint_mod
from ..concurrency import _run_concurrent
from ..tools import (
    _fetch_job_details_sorted,
    _fetch_jobs_list,
    _format_job_detail,
    _format_results_table,
    create_pinpoint_jobs,
    resolve_base_patch,
    resolve_exp_patches,
    resolve_patch_filter,
)

from ._shared import _resolve_repo, _text_result

# Argument documentation lives on the argument (Annotated[..., Field(...)]) so a
# client sends it as the parameter's own schema description rather than leaving
# the model to match a prose line against a signature by name. These recur
# across the pinpoint tools; the rest are inline at their parameter.
JOB_URLS_ARG = (
    "one or more Pinpoint job URLs or IDs, space-separated, e.g."
    ' "14cc0d73090000 12fd3dd7090000"'
)
PATCH_ARG = (
    "filter by Gerrit CL -- any URL form, change ID, or crrev."
    ' "auto" detects from the current branch; "none" clears the filter.'
)
BENCHMARK_ARG = (
    "filter by benchmark name or alias:"
    ' "js3" (jetstream-main.crossbench), "js2" (jetstream2.crossbench),'
    ' "sp3" (speedometer3.crossbench)'
)
BOT_ARG = (
    "filter by bot configuration name or alias:"
    ' "linux" (linux-r350-perf), "m1" (mac-m1_mini_2020-perf),'
    ' "m2" (mac-m2-pro-perf), "m3" (mac-m3-pro-perf),'
    ' "m4" (mac-m4-mini-perf), "m4pro" (mac-m4-pro-perf),'
    ' "win10" (win-10-perf)'
)


def _format_job_list(jobs: list[dict]) -> str:
    """Format job list as compact text (mirrors pp's list-jobs output)."""
    import concurrent.futures

    patches = [j.get("experiment_patch") or "" for j in jobs]
    with concurrent.futures.ThreadPoolExecutor() as ex:
        subjects = list(ex.map(pinpoint_mod.subject_or_none, patches))

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


def register(mcp: FastMCP, *, default_user: bool = True) -> None:
    @mcp.tool()
    def pinpoint_show_job(
        job_urls: Annotated[str, Field(description=JOB_URLS_ARG)],
    ) -> CallToolResult:
        """Fetch and display key information about one or more Pinpoint jobs."""
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
        job_urls: Annotated[str, Field(description=JOB_URLS_ARG)],
        reason: Annotated[str, Field(description="cancellation reason")] = "Cancelled",
    ) -> CallToolResult:
        """Cancel one or more Pinpoint jobs. Requires luci-auth login."""
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
        count: Annotated[int, Field(description="number of jobs to return")] = 20,
        user: Annotated[
            str | None,
            Field(description="user email (default: the current luci-auth user)"),
        ] = None,
        patch: Annotated[str | None, Field(description=PATCH_ARG)] = None,
        status: Annotated[
            str | None,
            Field(
                description=(
                    "filter by status: Completed, Running, Failed, Cancelled, Queued"
                )
            ),
        ] = None,
        benchmark: Annotated[str | None, Field(description=BENCHMARK_ARG)] = None,
        bot: Annotated[str | None, Field(description=BOT_ARG)] = None,
        since: Annotated[
            str,
            Field(
                description=(
                    "only show jobs created after this date. Accepts natural"
                    ' language ("2 weeks ago", "yesterday") or ISO dates; "all"'
                    " disables the cutoff."
                )
            ),
        ] = "one month ago",
    ) -> CallToolResult:
        """List recent Pinpoint jobs for a user, newest first. CQ jobs are excluded.

        Requires luci-auth login when user is not specified:
          luci-auth login -scopes https://www.googleapis.com/auth/userinfo.email

        All filters are ANDed together.
        """
        if not default_user and not user:
            return _text_result(
                "Error: pass an explicit user= (the default-user fallback is "
                "disabled in this deployment)."
            )
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
        job_urls: Annotated[str, Field(description=JOB_URLS_ARG)] = "",
        use_cas: Annotated[
            bool,
            Field(
                description=(
                    "fetch raw per-run values from CAS isolates instead of the"
                    " histogram HTML. Slower, but surfaces richer sub-metrics for"
                    " JetStream (Score, First, Average, Worst4 per story)."
                    " Requires: gcloud auth application-default login"
                )
            ),
        ] = False,
        recent: Annotated[
            int | None,
            Field(
                description=(
                    "show results for the N most recent completed jobs for the"
                    " current user. Can be combined with job_urls."
                )
            ),
        ] = None,
        patch: Annotated[str | None, Field(description=PATCH_ARG)] = None,
        status: Annotated[
            str | None,
            Field(
                description=(
                    "filter by status, in addition to the default Completed filter"
                )
            ),
        ] = None,
        benchmark: Annotated[str | None, Field(description=BENCHMARK_ARG)] = None,
        bot: Annotated[str | None, Field(description=BOT_ARG)] = None,
        since: Annotated[
            str | None,
            Field(
                description=(
                    'only include jobs after this date (default: "one month ago"'
                    " when filters are used). Accepts natural language or ISO"
                    ' dates; "all" for no limit.'
                )
            ),
        ] = None,
    ) -> CallToolResult:
        """Show a base-vs-experiment comparison table for a Pinpoint job.

        One row per metric: base mean±stdev, exp mean±stdev, %change, p-value
        (Mann-Whitney U, α=0.01), direction (↑improved/↓regressed).
        Sorted by %change descending.

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

        if not default_user and (recent or has_filters):
            return _text_result(
                "Error: listing jobs by recency/filters resolves to the "
                "logged-in account, which is disabled in this deployment; "
                "pass explicit job_urls instead."
            )

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
        benchmark: Annotated[
            str,
            Field(
                description=(
                    "space-separated benchmark names or aliases:"
                    ' "js3" (jetstream-main.crossbench, story JetStream),'
                    ' "js2" (jetstream2.crossbench, story JetStream2),'
                    ' "sp3" (speedometer3.crossbench, story Speedometer3)'
                )
            ),
        ] = "js3 sp3",
        configuration: Annotated[
            str,
            Field(
                description=(
                    "space-separated bot config(s) or alias(es), defaulting to"
                    ' two arm generations: "linux" (linux-r350-perf),'
                    ' "m1" (mac-m1_mini_2020-perf), "m2" (mac-m2-pro-perf),'
                    ' "m3" (mac-m3-pro-perf), "m4" (mac-m4-mini-perf),'
                    ' "m4pro" (mac-m4-pro-perf), "macintel" (mac-intel-perf),'
                    ' "win10" (win-10-perf),'
                    ' "win11" (win-11-perf, same R350 silicon as "linux")'
                )
            ),
        ] = " ".join(pinpoint_mod.DEFAULT_CONFIGURATIONS),
        exp_patch: Annotated[
            str,
            Field(
                description=(
                    "REQUIRED experiment patch. One of:"
                    ' "auto" to auto-detect from the current git branch\'s'
                    " Gerrit CL (fails if no CL is found);"
                    ' "none" for no patch (flag-only or hash-only comparisons);'
                    " or an explicit Gerrit CL URL, change ID, or crrev/c/N"
                )
            ),
        ] = "auto",
        story: Annotated[
            str | None,
            Field(
                description=("story within the benchmark (overrides the alias default)")
            ),
        ] = None,
        story_tags: Annotated[
            str | None,
            Field(description="comma-separated story tags to select stories"),
        ] = None,
        base_git_hash: Annotated[
            str | None,
            Field(
                description=(
                    "git hash for the base build (default: the auto-detected"
                    " latest CI build)"
                )
            ),
        ] = None,
        exp_git_hash: Annotated[
            str | None,
            Field(
                description=(
                    "git hash for the experiment build (default: the"
                    " auto-detected latest CI build)"
                )
            ),
        ] = None,
        base_patch: Annotated[
            str | None,
            Field(
                description=(
                    "Gerrit patch for base -- change ID, crrev/c/12345, or full"
                    ' URL. "parent" detects the parent CL from the current'
                    " branch's upstream branch, for measuring a stacked CL"
                    " against its parent."
                )
            ),
        ] = None,
        base_js_flags: Annotated[
            str | None,
            Field(
                description=(
                    'V8 flags for base, passed as --js-flags="...", e.g. "--turbofan"'
                )
            ),
        ] = None,
        exp_js_flags: Annotated[
            str | None,
            Field(description="V8 flags for the experiment, same format"),
        ] = None,
        repeat: Annotated[
            int, Field(description="number of bot runs per variant")
        ] = 150,
        bug_id: Annotated[
            int | None,
            Field(description="buganizer issue ID to associate with the job"),
        ] = None,
        v8_repo_path: Annotated[
            str | None,
            Field(
                description=(
                    'absolute path to the v8 repo for "auto" patch detection'
                    " (default: the worktree selected via"
                    " repo_git_worktree_select, else the configured v8 repo)"
                )
            ),
        ] = None,
    ) -> CallToolResult:
        """Create Pinpoint A/B try jobs. Requires luci-auth login.

        Creates one job per combination of benchmark x configuration. Pass
        multiple space-separated values to create jobs in bulk, e.g.
        benchmark="js3 sp3" configuration="m1 m4" creates 4 jobs.
        """
        repo_path = v8_repo_path or str(_resolve_repo("v8"))
        jobs = create_pinpoint_jobs(
            benchmarks=benchmark.split(),
            configurations=configuration.split(),
            story=story,
            story_tags=story_tags,
            base_git_hash=base_git_hash,
            exp_git_hash=exp_git_hash,
            base_patch=resolve_base_patch(base_patch, cwd=repo_path),
            exp_patches=resolve_exp_patches([exp_patch], cwd=repo_path),
            base_js_flags=base_js_flags,
            exp_js_flags_list=[exp_js_flags],
            repeat=repeat,
            bug_id=bug_id,
        )
        return _text_result("\n\n".join(_format_job_detail(j) for j in jobs))
