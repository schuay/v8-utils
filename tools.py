"""MCP tool definitions for v8-utils."""

from mcp.server.fastmcp import FastMCP

import config
import pinpoint

mcp = FastMCP("v8-utils")


@mcp.tool()
def pinpoint_show_job(job_url: str) -> dict:
    """Fetch and display key information about a Pinpoint job.

    job_url: Pinpoint job URL or job ID, e.g.
             https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000
    """
    job_id = pinpoint.job_id_from_url(job_url)
    data = pinpoint.fetch_job(job_id)
    args = data.get("arguments", {})
    return {
        "job_id":                data.get("job_id"),
        "name":                  data.get("name"),
        "status":                data.get("status"),
        "user":                  data.get("user"),
        "created":               data.get("created"),
        "updated":               data.get("updated"),
        "comparison_mode":       data.get("comparison_mode"),
        "configuration":         data.get("configuration"),
        "benchmark":             args.get("benchmark"),
        "story":                 args.get("story"),
        "base_git_hash":         args.get("base_git_hash"),
        "end_git_hash":          args.get("end_git_hash"),
        "experiment_patch":      args.get("experiment_patch"),
        "base_extra_args":       args.get("base_extra_args"),
        "experiment_extra_args": args.get("experiment_extra_args"),
        "difference_count":      data.get("difference_count"),
        "exception":             data.get("exception"),
        "bug_id":                data.get("bug_id"),
        "results_url":           data.get("results_url"),
        "bots":                  data.get("bots"),
    }


@mcp.tool()
def pinpoint_list_jobs(
    count: int = 20,
    user: str | None = None,
    filter: str | None = None,
) -> list[dict]:
    """List recent Pinpoint jobs for a user, newest first. CQ jobs are excluded.

    Requires luci-auth login when user is not specified:
      luci-auth login -scopes https://www.googleapis.com/auth/userinfo.email

    count:  number of jobs to return (default: 20)
    user:   user email (default: current luci-auth user)
    filter: optional client-side "key=value" filter, e.g.:
              "status=Completed"
              "benchmark=jetstream2"
              "configuration=linux-r350-perf"
              "comparison_mode=try"

    Each entry includes job_id, url, name, status, created, configuration,
    benchmark, story, base/experiment patch and extra_args, difference_count.
    """
    if user is None:
        user = config.load().user or pinpoint.get_current_user_email()
    return [pinpoint.summarise_job(j) for j in pinpoint.fetch_jobs(user, count, filter)]


@mcp.tool()
def pinpoint_get_raw_values(job_url: str) -> list[dict]:
    """Return per-run measurement values for a Pinpoint job.

    One row per (metric, bot run): metric, label, run_id, unit, value.
    run_id is a GUID consistent across all metrics in a run (join key).

    job_url: Pinpoint job URL or job ID
    """
    return pinpoint.fetch_raw_values(pinpoint.job_id_from_url(job_url))


@mcp.tool()
def pinpoint_show_results(job_url: str, show_all: bool = False) -> str:
    """Show a base-vs-experiment comparison table for a Pinpoint job.

    One row per metric: base mean±stdev, exp mean±stdev, %change, p-value,
    significance marker. Sorted by %change descending.

    show_all: if False (default), only show statistically significant results.
    job_url:  Pinpoint job URL or job ID
    """
    job_id = pinpoint.job_id_from_url(job_url)
    rows = pinpoint.pivot_results(job_id)
    if not rows:
        return "No results found."

    if not show_all:
        rows = [r for r in rows if r["significant"]]
    if not rows:
        return "No statistically significant results found."

    def pct(r: dict) -> float:
        bm = r["base_mean"] or 0
        return (r["exp_mean"] - bm) / bm * 100 if bm else 0

    rows.sort(key=pct, reverse=True)

    cells = []
    for r in rows:
        bm, bs = r["base_mean"] or 0, r["base_stdev"] or 0
        em, es = r["exp_mean"]  or 0, r["exp_stdev"]  or 0
        cells.append((
            r["name"],
            f"{bm:.3f} ±{bs:.3f}",
            f"{em:.3f} ±{es:.3f}",
            f"{pct(r):+.2f}%",
            f"{r['p_value']:.4f}",
            "*" if r["significant"] else "",
        ))

    hdrs = ("metric", "base mean±stdev", "exp mean±stdev", "chg%", "p", "sig")
    widths = [max(len(h), max(len(c[i]) for c in cells)) for i, h in enumerate(hdrs)]

    def fmt_row(cols: tuple) -> str:
        return "  ".join(
            c.ljust(widths[i]) if i == 0 else c.rjust(widths[i])
            for i, c in enumerate(cols)
        )

    sep = "-" * (sum(widths) + 2 * (len(widths) - 1))
    return "\n".join([
        f"base: {rows[0]['base_label']}",
        f"exp:  {rows[0]['exp_label']}",
        "",
        fmt_row(hdrs),
        sep,
        *[fmt_row(c) for c in cells],
    ])


@mcp.tool()
def pinpoint_create_job(
    benchmark: str,
    configuration: str,
    story: str | None = None,
    story_tags: str | None = None,
    base_git_hash: str = "HEAD",
    exp_git_hash: str = "HEAD",
    base_patch: str | None = None,
    exp_patch: str | None = None,
    base_js_flags: str | None = None,
    exp_js_flags: str | None = None,
    repeat: int = 100,
    bug_id: int | None = None,
) -> dict:
    """Create a new Pinpoint A/B try job. Requires luci-auth login.

    benchmark:      benchmark name or alias:
                      "js3"  → jetstream-main.crossbench (story: JetStream)
    configuration:  bot config or alias:
                      "linux" → linux-r350-perf
                      "macm4" → mac-m4-mini-perf
    story:          story within the benchmark (overrides alias default)
    story_tags:     comma-separated story tags to select stories
    base_git_hash:  git hash for the base build (default: HEAD)
    exp_git_hash:   git hash for the experiment build (default: HEAD)
    base_patch:     Gerrit patch for base — change ID, crrev/c/12345, or full URL
    exp_patch:      Gerrit patch for experiment — same formats
    base_js_flags:  V8 flags for base, passed as --js-flags="...", e.g. "--turbofan"
    exp_js_flags:   V8 flags for experiment, same format
    repeat:         number of bot runs per variant (default: 100)
    bug_id:         buganizer issue ID to associate with the job
    """
    return pinpoint.create_job(
        benchmark=benchmark,
        configuration=configuration,
        story=story,
        story_tags=story_tags,
        base_git_hash=base_git_hash,
        exp_git_hash=exp_git_hash,
        base_patch=base_patch,
        exp_patch=exp_patch,
        base_js_flags=base_js_flags,
        exp_js_flags=exp_js_flags,
        repeat=repeat,
        bug_id=bug_id,
    )
