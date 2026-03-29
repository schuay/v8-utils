"""MCP tool definitions for v8-utils."""

import os
import re as _re
import shutil
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from . import config
from . import gerrit as gerrit_tools
from . import jsb as jsb_module
from . import perf as perf_tools
from . import pinpoint
from . import worktree as worktree_mod
from .tools import (
    _fetch_job_detail,
    _fetch_job_details_sorted,
    _fetch_jobs_list,
    _format_job_detail,
    _format_results_table,
    _results_header,
    _run_concurrent,
    chat_notify_watching,
    create_pinpoint_jobs,
    resolve_exp_patches,
    resolve_patch_filter,
)

mcp = FastMCP("v8-utils", log_level="WARNING")

_STARTUP_MTIME = os.path.getmtime(__file__)


def _check_stale() -> str:
    try:
        if os.path.getmtime(__file__) != _STARTUP_MTIME:
            return (
                "[WARNING: v8-utils was upgraded — "
                "restart the MCP server to use the new version]\n\n"
            )
    except OSError:
        pass
    return ""


def _text_result(text: str) -> CallToolResult:
    """Return a CallToolResult with both content and structuredContent.

    Setting structuredContent.content makes Claude Code display the text
    with proper newlines instead of a collapsed JSON blob (see
    anthropics/claude-code#9962).
    """
    return CallToolResult(
        content=[TextContent(type="text", text=_check_stale() + text)],
    )


def _paginate(lines: list[str], offset: int, limit: int) -> tuple[list[str], int, int]:
    """Apply offset/limit pagination to a list of lines.

    offset: 0-based line offset. Negative values count from the end
            (e.g. -100 means last 100 lines).
    limit:  max lines to return.

    Returns (selected_lines, resolved_offset, total).
    """
    total = len(lines)
    if offset < 0:
        offset = max(total + offset, 0)
    selected = lines[offset : offset + limit]
    return selected, offset, total


def _paginate_result(
    lines: list[str], offset: int, limit: int, *, numbered: bool = False
) -> str:
    """Paginate lines and format with optional line numbers and truncation msg."""
    selected, offset, total = _paginate(lines, offset, limit)
    if numbered:
        result = "\n".join(
            f"{i + offset + 1:6}\t{line}" for i, line in enumerate(selected)
        )
    else:
        result = "\n".join(selected)
    if offset + limit < total:
        result += (
            f"\n(showing lines {offset + 1}\u2013{offset + len(selected)}"
            f" of {total}; use offset/limit to paginate)"
        )
    return result


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
    # Display oldest first (API returns newest first).
    jobs.reverse()
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


@mcp.tool()
def pinpoint_show_results(
    job_urls: str = "",
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

    One row per metric: base mean±stdev, exp mean±stdev, %change, p-value
    (Mann-Whitney U, α=0.01), direction (↑improved/↓regressed).
    Sorted by %change descending.

    job_urls:  one or more Pinpoint job URLs or IDs, space-separated
               (e.g. "14cc0d73090000 12fd3dd7090000")
    show_all:  if False (default), only show statistically significant results.
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
        job_ids.extend(pinpoint.job_id_from_url(u) for u in job_urls.split())

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
        since_dt = pinpoint.parse_since(since_str) if since_str else None
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
            jid, show_all, use_cas, job=detail_map.get(jid)
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

    Note: d8 prints trace output (--trace-turbo, --print-code, etc.) to
    stdout, not stderr. Use stdout_file to capture traces to a file.

    args:        arguments to pass to d8 (e.g. ["--prof", "script.js"])
    build:       build directory name under repos["v8"]/out (default: config default_build)
    cwd:         working directory for d8 (default: repos["v8"])
    timeout:     max seconds before killing the process (default: 60)
    stdout_file: redirect stdout to this file path instead of capturing
    stderr_file: redirect stderr to this file path instead of capturing
    """
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
    lineitems: list[str] | None = None,
    builds: list[str] = [],
    runs: int = 5,
    suite: str = "js3",
    show_all: bool = False,
    perf: bool = False,
    perf_upload: bool = False,
) -> CallToolResult:
    """Run a JetStream2/3 story with one or more JS shell builds and return scores.

    lineitems: benchmark story names, e.g. ["regexp-octane", "chai-wtb"].
               Omit to run the full suite.
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

    Returns a comparison table with mean, stdev, delta, p-value
    (Welch's t-test), and confidence (high/medium/low) per metric.
    """
    cfg = config.load()
    js3 = suite.lower() != "js2"
    suite_dir = cfg.repos["js3"].path if js3 else cfg.repos["js2"].path
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
                lineitems,
                cfg.v8_out,
                cfg.perf_script,
                upload=perf_upload,
            )
        )

    results = [
        jsb_module.run_variant(v, suite_dir, lineitems, runs, js3, cfg.v8_out)
        for v in variants
    ]

    return _text_result(
        jsb_module.format_table(
            lineitems, suite_label, runs, variants, results, show_all=show_all
        )
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


@mcp.tool()
def gerrit_list_cls(query: str, limit: int = 25) -> CallToolResult:
    """Search for Gerrit CLs on chromium-review.googlesource.com.

    Returns a compact summary of matching CLs: number, subject, status,
    owner, labels (Code-Review, Commit-Queue scores), reviewers, and
    attention set.

    "self" in queries is resolved to the configured user email.

    query: Gerrit search query, e.g.:
      "owner:self status:open project:v8/v8"
      "reviewer:self -owner:self status:open project:v8/v8"
      "owner:self status:merged after:2026-03-01"
      "hashtag:compiler project:v8/v8 status:open"
    limit: max results (default 25)
    """
    cls = gerrit_tools.list_cls(query, limit=limit)
    if not cls:
        return _text_result(f"No CLs found for query: {query}")
    return _text_result(_format_cl_list(cls))


def _format_cl_list(cls: list[dict]) -> str:
    """Format a list of compact change dicts into readable text."""
    blocks = []
    for cl in cls:
        # Label scores
        label_parts = []
        for label, votes in cl.get("labels", {}).items():
            scores = " ".join(f"{'+' if v > 0 else ''}{v}" for _, v in votes)
            # Shorten well-known labels
            short = label.replace("Code-Review", "CR").replace("Commit-Queue", "CQ")
            label_parts.append(f"{short}:{scores}")
        labels_str = f"  [{', '.join(label_parts)}]" if label_parts else ""

        wip = " (WIP)" if cl.get("wip") else ""
        comments = ""
        if cl.get("unresolved_comments"):
            comments = f"  {cl['unresolved_comments']} unresolved"

        line1 = f'{cl["number"]}  {cl["status"]}{wip}  "{cl["subject"]}"'
        line2 = (
            f"  {cl['owner']}  "
            f"+{cl['insertions']}/-{cl['deletions']}  "
            f"ps{cl.get('patchset', '?')}  "
            f"updated {cl['updated'][:10]}"
            f"{labels_str}{comments}"
        )

        lines = [line1, line2]

        if cl.get("reviewers"):
            lines.append(f"  reviewers: {', '.join(cl['reviewers'])}")

        if cl.get("attention"):
            attn = [f"{a['email']} ({a['reason']})" for a in cl["attention"]]
            lines.append(f"  attention: {', '.join(attn)}")

        blocks.append("\n".join(lines))

    header = f"{len(cls)} CL(s) found\n"
    return header + "\n\n".join(blocks)


# ── CQ / Buildbucket tools ──────────────────────────────────────────────────


def _bb_run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a bb CLI command, raising ValueError on missing binary or auth."""
    bb = shutil.which("bb")
    if bb is None:
        raise ValueError(
            "bb (Buildbucket CLI) not found. "
            "Install depot_tools and ensure it is on PATH."
        )
    r = subprocess.run([bb, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        stderr = r.stderr.strip()
        if "Login required" in stderr or "not logged in" in stderr:
            raise ValueError(f"bb auth required: run 'bb auth-login'.\n{stderr}")
        if stderr:
            raise ValueError(f"bb {args[0]} failed: {stderr}")
    return r


def _parse_bb_jsonl(stdout: str) -> list[dict]:
    """Parse bb JSONL output (one JSON object per line)."""
    import json

    builds = []
    for line in stdout.strip().splitlines():
        line = line.strip()
        if line:
            try:
                builds.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return builds


def _bb_builder_name(build: dict) -> str:
    """Extract 'project/bucket/builder' from a build dict."""
    b = build.get("builder", {})
    return "/".join(
        p for p in [b.get("project"), b.get("bucket"), b.get("builder")] if p
    )


def _bb_categorize(builds: list[dict]) -> dict[str, list[dict]]:
    """Group builds by status category, deduplicating by builder name.

    When multiple CQ attempts produce builds for the same builder,
    keeps only the latest (highest build id) per builder name per category.
    """
    cats: dict[str, list[dict]] = {
        "SUCCESS": [],
        "FAILURE": [],
        "INFRA_FAILURE": [],
        "RUNNING": [],
        "CANCELED": [],
    }
    for b in builds:
        status = b.get("status", "")
        if status in ("STARTED", "SCHEDULED"):
            cats["RUNNING"].append(b)
        elif status in cats:
            cats[status].append(b)
    # Deduplicate: keep latest build per builder name in each category
    for key in cats:
        seen: dict[str, dict] = {}
        for b in cats[key]:
            name = _bb_builder_name(b)
            if name not in seen or str(b.get("id", "")) > str(seen[name].get("id", "")):
                seen[name] = b
        cats[key] = list(seen.values())
    return cats


def _bb_leaf_failures(build: dict) -> list[str]:
    """Return leaf failed step names from a build (which already has steps)."""
    failed = [s for s in build.get("steps", []) if s.get("status") == "FAILURE"]
    names = {s["name"] for s in failed}
    return [
        s["name"]
        for s in failed
        if not any(o != s["name"] and o.startswith(s["name"] + "|") for o in names)
    ]


def _format_cq_overview(
    cl_number: str,
    patchset: int,
    cats: dict[str, list[dict]],
) -> str:
    """Format CQ results as a compact overview (no logs)."""
    n_pass = len(cats["SUCCESS"])
    n_fail = len(cats["FAILURE"])
    n_infra = len(cats["INFRA_FAILURE"])
    n_run = len(cats["RUNNING"])
    n_cancel = len(cats["CANCELED"])
    total = n_pass + n_fail + n_infra + n_run + n_cancel

    parts = []
    if n_pass:
        parts.append(f"{n_pass} passed")
    if n_fail:
        parts.append(f"{n_fail} failed")
    if n_infra:
        parts.append(f"{n_infra} infra failures")
    extra = ""
    if n_run:
        extra += f"; {n_run} running"
    if n_cancel:
        extra += f"; {n_cancel} canceled"

    lines = [
        f"CQ results for {cl_number}/{patchset}",
        "",
        f"Summary: {', '.join(parts)} (of {total} builds{extra})",
    ]

    if cats["RUNNING"]:
        lines.append("")
        lines.append("RUNNING:")
        for b in cats["RUNNING"]:
            lines.append(f"  {_bb_short_name(b)}")

    if cats["INFRA_FAILURE"]:
        lines.append("")
        lines.append("INFRA_FAILURE:")
        for b in cats["INFRA_FAILURE"]:
            sm = b.get("summaryMarkdown", "")
            detail = f"  ({sm[:200]})" if sm else ""
            lines.append(f"  {_bb_short_name(b)}{detail}")

    if cats["FAILURE"]:
        lines.append("")
        lines.append("FAILED:")
        for b in cats["FAILURE"]:
            step_names = _bb_leaf_failures(b)
            if step_names:
                steps_str = ", ".join(step_names[:3])
                if len(step_names) > 3:
                    steps_str += f", +{len(step_names) - 3} more"
                lines.append(f"  {_bb_short_name(b)}  ({steps_str})")
            else:
                lines.append(f"  {_bb_short_name(b)}")

    if n_pass:
        lines.append("")
        lines.append(f"{n_pass} passed (not shown)")

    lines.append("")
    lines.append("Use builder=<name> to zoom into a specific bot's failure logs.")

    return "\n".join(lines)


def _bb_short_name(build: dict) -> str:
    """Extract just the builder name (without project/bucket prefix)."""
    return build.get("builder", {}).get("builder", _bb_builder_name(build))


def _dedup_lines(text: str) -> str:
    """Collapse consecutive duplicate lines, showing count."""
    lines = text.splitlines()
    if not lines:
        return text
    out: list[str] = []
    prev = lines[0]
    count = 1
    for line in lines[1:]:
        if line == prev:
            count += 1
        else:
            out.append(prev if count == 1 else f"{prev}  (x{count})")
            prev = line
            count = 1
    out.append(prev if count == 1 else f"{prev}  (x{count})")
    return "\n".join(out)


_RE_INFRA_LOG = _re.compile(r"^\[?[DIW]\d{4}-\d{2}-\d{2}T|^I\d{4} |^INFO:|^\s*$")


def _strip_trailing_infra(lines: list[str]) -> list[str]:
    """Remove trailing infrastructure log lines (swarming, CAS, etc.)."""
    # Walk backwards, dropping infra log lines
    i = len(lines)
    while i > 0 and _RE_INFRA_LOG.match(lines[i - 1]):
        i -= 1
    return lines[:i] if i < len(lines) else lines


def _clean_log(text: str) -> str:
    """Light cleanup of a build log: dedup lines, strip PASS/infra noise."""
    lines = [l for l in text.splitlines() if not l.rstrip().endswith(": PASS")]
    lines = _strip_trailing_infra(lines)
    return _dedup_lines("\n".join(lines))


def _format_cq_builder_detail(
    build: dict,
) -> str:
    """Fetch and format failure logs for a single builder."""
    builder = _bb_short_name(build)
    build_id = str(build.get("id", ""))
    step_names = _bb_leaf_failures(build)

    lines = [f"Failure details for {builder} (build {build_id})", ""]

    if not step_names:
        lines.append("(no failed steps found)")
        return "\n".join(lines)

    def fetch_log(step_name: str) -> tuple[str, str | None]:
        try:
            lr = _bb_run(["log", build_id, step_name, "stdout"], timeout=30)
            return step_name, lr.stdout
        except (ValueError, subprocess.TimeoutExpired):
            return step_name, None

    fns = [lambda s=s: fetch_log(s) for s in step_names]
    results = _run_concurrent(fns)

    for step_name, raw_log in results:
        lines.append(f"── {step_name} ──")
        if raw_log is None:
            lines.append("(log fetch failed or timed out)")
        else:
            lines.append(_clean_log(raw_log))
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def gerrit_cq(
    change: str,
    patchset: int,
    builder: str = "",
    offset: int = 0,
    limit: int = 200,
) -> CallToolResult:
    """Show CQ bot results for a Gerrit CL.

    Without builder: returns a compact overview of which bots passed/failed.
    With builder: zooms into that bot's failure logs (with backtraces).

    change:    CL number or Gerrit URL (e.g. "7706944" or full URL)
    patchset:  patchset number
    builder:   builder name to zoom into (substring match, e.g. "linux64_rel")
    offset:    line offset into builder detail output (default 0)
    limit:     max lines to return for builder detail (default 200)
    """
    from .pinpoint_cache import parse_patch_fields

    # Parse CL number from URL or bare number
    _, cl_number, _ = parse_patch_fields(change)
    if not cl_number:
        # Try bare number
        stripped = change.strip().split("/")[0]
        if stripped.isdigit():
            cl_number = stripped
        else:
            return _text_result(f"Error: cannot parse CL number from {change!r}")

    cl_spec = f"chromium-review.googlesource.com/c/v8/v8/+/{cl_number}/{patchset}"

    try:
        r = _bb_run(["ls", "-cl", cl_spec, "-json", "-steps"])
    except ValueError as e:
        return _text_result(f"Error: {e}")

    builds = _parse_bb_jsonl(r.stdout)
    if not builds:
        return _text_result(f"No builds found for CL {cl_number} patchset {patchset}.")

    cats = _bb_categorize(builds)

    if not builder:
        return _text_result(_format_cq_overview(cl_number, patchset, cats))

    # Zoom into a specific builder
    matches = [
        b for b in cats["FAILURE"] if builder.lower() in _bb_builder_name(b).lower()
    ]
    if not matches:
        all_failed = [_bb_short_name(b) for b in cats["FAILURE"]]
        return _text_result(
            f"No failed builder matching {builder!r}.\n"
            f"Failed builders: {', '.join(all_failed) or '(none)'}"
        )
    if len(matches) > 1:
        names = [_bb_short_name(b) for b in matches]
        return _text_result(
            f"Multiple builders match {builder!r}: {', '.join(names)}\n"
            f"Be more specific."
        )

    full = _format_cq_builder_detail(matches[0])
    return _text_result(_paginate_result(full.splitlines(), offset, limit))


# ── repo tools ───────────────────────────────────────────────────────────────

_MAX_READ_LINES = 2000
_MAX_GREP_MATCHES = 100


def _resolve_repo(repo: str) -> Path:
    """Resolve a repo name to its configured path, or raise ValueError."""
    cfg = config.load()
    entry = cfg.repos.get(repo)
    if entry is None:
        valid = ", ".join(sorted(cfg.repos))
        raise ValueError(f"Unknown repo {repo!r}. Configured repos: {valid}")
    if not entry.path.is_dir():
        raise ValueError(f"Repo {repo!r} path does not exist: {entry.path}")
    return entry.path


def _register_repo_resources():
    """Register MCP resources for configured repos."""
    cfg = config.load()
    for alias, entry in cfg.repos.items():
        if not entry.path.is_dir():
            continue
        desc = entry.desc or str(entry.path)

        def _make_resource(a: str, d: str, p: str):
            @mcp.resource(f"repo://{a}", name=a, description=d)
            def _repo_resource():
                return p

            return _repo_resource

        _make_resource(alias, desc, str(entry.path))


_register_repo_resources()


def _repo_summary() -> str:
    """One-line summary of configured repos for embedding in tool descriptions."""
    cfg = config.load()
    parts = []
    for alias, entry in cfg.repos.items():
        if entry.path.is_dir():
            parts.append(f"{alias} ({entry.desc})" if entry.desc else alias)
    return ", ".join(parts)


_REPOS_LINE = _repo_summary()


@mcp.tool()
def repo_git_show(
    repo: str,
    path: str,
    offset: int = 0,
    limit: int = _MAX_READ_LINES,
    ref: str | None = None,
) -> CallToolResult:
    """Read a file from a related source repo (git show / working tree).

    repo:   repo name from the repo:// MCP resources
    path:   file path relative to the repo root, e.g. "runtime/RegExp.cpp"
    offset: 0-based line offset to start reading from (default: 0)
    limit:  max lines to return (default: 2000)
    ref:    git ref to read from (e.g. commit hash, branch, tag).
            If omitted, reads from the working tree.
    """
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
    return _text_result(_paginate_result(lines, offset, limit, numbered=True))


@mcp.tool(
    description=(
        "Search for a pattern in a related source repo using git grep.\n"
        "\n"
        f"Configured repos: {_REPOS_LINE}\n"
        "\n"
        "repo:    repo name (see list above)\n"
        "pattern: regex pattern to search for\n"
        'glob:    optional file glob filter, e.g. "*.cpp" or "*.{h,cpp}"\n'
        "context: lines of context around each match (default: 0)\n"
        "limit:   max matches to return (default: 100)\n"
        "ref:     git ref to search in (e.g. commit hash, branch, tag).\n"
        "         If omitted, searches the working tree."
    )
)
def repo_git_grep(
    repo: str,
    pattern: str,
    glob: str | None = None,
    context: int = 0,
    limit: int = _MAX_GREP_MATCHES,
    ref: str | None = None,
) -> CallToolResult:
    root = _resolve_repo(repo)
    cmd = ["git", "grep", "-n", "--no-color", "-E"]
    if context > 0:
        cmd.append(f"-C{context}")
    cmd.append(pattern)
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


_MAX_LS_FILES = 500
_MAX_LOG_LINES = 2000


@mcp.tool()
def repo_git_find(
    repo: str,
    glob: str,
    limit: int = _MAX_LS_FILES,
    ref: str | None = None,
) -> CallToolResult:
    """List files in a related source repo matching a glob pattern (git ls-files).

    repo:   repo name from the repo:// MCP resources
    glob:   file glob pattern, e.g. "*.cpp", "src/**/*.h", "runtime/RegExp*"
    limit:  max files to return (default: 500)
    ref:    git ref to list from (e.g. commit hash, branch, tag).
            If omitted, lists from the working tree.
    """
    root = _resolve_repo(repo)
    if ref:
        cmd = ["git", "ls-tree", "-r", "--name-only", ref, "--", glob]
    else:
        cmd = ["git", "ls-files", "--", glob]

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

    if not collected:
        return _text_result("No files found.")

    if len(collected) > limit:
        result = "\n".join(collected[:limit])
        result += f"\n(truncated — showing first {limit} files)"
    else:
        result = "\n".join(collected)
    return _text_result(result)


@mcp.tool()
def repo_git_log(
    repo: str,
    path: str | None = None,
    ref: str | None = None,
    limit: int = 20,
    grep: str | None = None,
) -> CallToolResult:
    """Show git log in a related source repo.

    repo:   repo name from the repo:// MCP resources
    path:   optional file path to show history for
    ref:    git ref to start from (default: HEAD)
    limit:  max commits to return (default: 20)
    grep:   optional pattern to filter commit messages
    """
    root = _resolve_repo(repo)
    cmd = [
        "git",
        "log",
        f"-{limit}",
        "--format=%h %as %an  %s",
    ]
    if grep:
        cmd.extend(["--grep", grep, "-i"])
    if ref:
        cmd.append(ref)
    if path:
        cmd.extend(["--", path])

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=root,
    )
    if proc.returncode != 0:
        raise ValueError(f"git log failed: {proc.stderr.strip()[:500]}")
    result = proc.stdout.strip()
    if not result:
        return _text_result("No commits found.")
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
    Sorted by self_pct descending.

    Typical workflow: perf_hotspots → perf_flamegraph → perf_annotate.

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


# ---------------------------------------------------------------------------
# d8 trace index
# ---------------------------------------------------------------------------

# Each pattern: (compiled regex, category, group index for label extraction)
_TRACE_PATTERNS: list[tuple[_re.Pattern[str], str, int | None]] = [
    # Turbofan compilation boundaries
    (
        _re.compile(r"^Begin compiling method (.+) using TurboFan"),
        "turbofan",
        1,
    ),
    (
        _re.compile(r"^Finished compiling method (.+) using TurboFan"),
        "turbofan",
        1,
    ),
    # Maglev compilation boundary
    (
        _re.compile(r"^Compiling 0x[0-9a-f]+ <JSFunction (\S+) .+> with Maglev"),
        "maglev",
        1,
    ),
    # Maglev inlining (must be before generic phase pattern)
    (
        _re.compile(
            r"^----- Inlining 0x[0-9a-f]+ <SharedFunctionInfo (\S+)> with bytecode"
        ),
        "maglev-inline",
        1,
    ),
    # Turbofan / Turboshaft / Maglev phases: "----- <phase> -----"
    # Matches graph phases, schedule, instruction sequence, bytecode array, etc.
    (_re.compile(r"^----- (.+?) -----\s*$"), "phase", 1),
    # trace-opt: marking for optimization
    (
        _re.compile(
            r"^\[marking 0x[0-9a-f]+ <JSFunction (\S+) .+> for optimization to (\S+),"
        ),
        "opt",
        None,  # custom extraction below
    ),
    # trace-opt: compiling method
    (
        _re.compile(
            r"^\[compiling method 0x[0-9a-f]+ <JSFunction (\S+) .+> \(target (\S+)\)"
        ),
        "compile",
        None,
    ),
    # trace-opt: completed compiling
    (
        _re.compile(
            r"^\[completed compiling 0x[0-9a-f]+ <JSFunction (\S+) .+> \(target (\S+)\)"
        ),
        "compiled",
        None,
    ),
    # trace-deopt: bailout
    (
        _re.compile(
            r"^\[bailout \(kind: ([^,]+), reason: ([^)]+)\): begin\. deoptimizing 0x[0-9a-f]+ <JSFunction (\S+)"
        ),
        "deopt",
        None,
    ),
    # print-code: Code object header
    (
        _re.compile(r"^kind = (\S+)"),
        "code",
        1,
    ),
]


def _extract_label(pattern_idx: int, m: _re.Match[str]) -> str:
    """Extract a human-readable label from a regex match."""
    cat = _TRACE_PATTERNS[pattern_idx][1]
    if cat == "opt":
        return f"marking {m.group(1)} → {m.group(2)}"
    if cat in ("compile", "compiled"):
        return f"{m.group(1)} (target {m.group(2)})"
    if cat == "deopt":
        return f"{m.group(3)}: {m.group(2)} ({m.group(1)})"
    group_idx = _TRACE_PATTERNS[pattern_idx][2]
    if group_idx is not None:
        return m.group(group_idx)
    return m.group(0)


def _build_trace_index(path: str) -> str:
    """Scan a trace file and return a table of contents."""
    from pathlib import Path

    text = Path(path).read_text(errors="replace")
    lines = text.split("\n")

    entries: list[tuple[int, str, str]] = []  # (line_no, category, label)

    # Track current compilation context for indentation
    for i, line in enumerate(lines):
        for pat_idx, (pattern, cat, _) in enumerate(_TRACE_PATTERNS):
            m = pattern.match(line)
            if m:
                label = _extract_label(pat_idx, m)
                entries.append((i + 1, cat, label))
                break

    if not entries:
        return f"No trace sections found in {path} ({len(lines)} lines)"

    # Format output with indentation for phases within compilations
    out: list[str] = [f"{path} ({len(lines)} lines, {len(entries)} sections)"]
    out.append("")

    in_compilation = False
    for line_no, cat, label in entries:
        prefix = f"L{line_no:<8}"
        if cat in ("turbofan", "maglev"):
            if "Finished" in label or "completed" in label:
                in_compilation = False
                out.append(f"{prefix}[{cat}] Finished {label}")
            else:
                in_compilation = True
                out.append(f"{prefix}[{cat}] {label}")
        elif cat == "phase":
            indent = "  " if in_compilation else ""
            out.append(f"{prefix}{indent}[phase] {label}")
        elif cat == "maglev-inline":
            out.append(f"{prefix}  [inline] {label}")
        elif cat == "opt":
            out.append(f"{prefix}[opt] {label}")
        elif cat == "compile":
            out.append(f"{prefix}[compile] {label}")
        elif cat == "compiled":
            out.append(f"{prefix}[compiled] {label}")
        elif cat == "deopt":
            out.append(f"{prefix}[deopt] {label}")
        elif cat == "code":
            out.append(f"{prefix}[code] {label}")
        else:
            out.append(f"{prefix}[{cat}] {label}")

    return "\n".join(out)


@mcp.tool()
def d8_trace_index(path: str) -> CallToolResult:
    """Build a table of contents for a V8 trace file.

    Recognizes sections from --trace-turbo-graph, --print-maglev-graphs,
    --trace-maglev-graph-building, --trace-opt, --trace-deopt, and
    --print-code. Use the line numbers to navigate with read_around.

    path: path to the trace file
    """
    try:
        return _text_result(_build_trace_index(path))
    except FileNotFoundError:
        return _text_result(f"File not found: {path}")


# ---------------------------------------------------------------------------
# llvm-mca
# ---------------------------------------------------------------------------


# V8 print-opt-code format:
#   0x7fc5e000a500    80  453bd8               cmpl r11,r8
_RE_V8_PRINT_CODE = _re.compile(r"^0x[0-9a-f]+\s+[0-9a-f]+\s+[0-9a-f]+\s+(.*)")

# perf annotate format:
#      3.15 :   1d508c3:        testb  $0x8,(%rsi,%r14,1)
_RE_PERF_ANNOTATE = _re.compile(r"^\s*\d+\.\d+\s*:\s+[0-9a-f]+:\s+(.*)")

# GDB disassemble format (with optional => marker and /r hex bytes):
#    0x00005555555fc5c0 <Main()+0>:	push   rbp
# => 0x00005555555fdd64 <main+4>:	pop    rbp
#    0x00005555555fdd6a:	int3
#    0x00005555555fc5c0 <Main()+0>:	55                 	push   rbp   (with /r)
_RE_GDB_DISASM = _re.compile(
    r"^(?:=>)?\s*0x[0-9a-f]+"  # optional => marker, address
    r"(?:\s+<[^>]+>)?:\s+"  # optional <symbol+offset>, then colon
    r"(?:[0-9a-f]{2}(?:\s[0-9a-f]{2})*\s+)?"  # optional hex bytes (/r flag)
    r"(.*)"  # instruction
)

# V8 code comment / ANSI escape lines
_RE_V8_COMMENT = _re.compile(r"^\s*\[3[24]m|\s*\]")

# V8 uses a hybrid syntax: AT&T size suffixes (movl, addl) with Intel operand
# order. Strip the suffix so the Intel parser accepts them.
_RE_SIZE_SUFFIX = _re.compile(
    r"^(REX\.W\s+)?"  # optional REX.W prefix
    r"(j[a-z]+|set[a-z]+|mov[sz]?|lea|add|sub|cmp|test|and|or|xor|sar|shr|shl|"
    r"sal|inc|dec|neg|not|imul|idiv|mul|div|push|pop|call|ret|nop|"
    r"cmov[a-z]+)"
    r"([bwlq])\b",  # size suffix
    _re.IGNORECASE,
)

# Trailing annotations: "<+0x104>", "(comment)", ";; comment"
_RE_TRAILING_ANNOTATION = _re.compile(r"\s+<\+0x[0-9a-f]+>.*$|\s+\(.*\)\s*$|\s+;;.*$")

# REX.W prefix — strip it, the instruction works without it in the assembler
_RE_REX_PREFIX = _re.compile(r"^REX\.W\s+", _re.IGNORECASE)

# Absolute address as jump/call target: "jne 0x7fc5..." or "jne 1d50886" → "jne .L0"
_RE_ABS_JUMP = _re.compile(
    r"^(j[a-z]*|call)\s+(?:0x)?([0-9a-f]{4,})\s*$", _re.IGNORECASE
)


def _clean_asm_for_mca(raw: str) -> str:
    """Strip address/hex prefixes from V8 print-code or perf annotate output."""
    cleaned: list[str] = []
    v8_format = False
    for line in raw.splitlines():
        # V8 print-opt-code: "0xADDR  OFF  HEX  instruction"
        m = _RE_V8_PRINT_CODE.match(line)
        if m:
            v8_format = True
            cleaned.append(m.group(1))
            continue
        # perf annotate: "  pct : addr: instruction"
        m = _RE_PERF_ANNOTATE.match(line)
        if m:
            cleaned.append(m.group(1))
            continue
        # GDB wrapper lines
        if line.startswith("Dump of assembler code") or line.startswith(
            "End of assembler dump"
        ):
            continue
        # GDB disassemble: "   0xADDR <sym+off>:  instruction"
        m = _RE_GDB_DISASM.match(line)
        if m:
            instr = m.group(1).strip()
            if instr:
                cleaned.append(instr)
            continue
        # Skip ANSI escape lines (V8 code comments with [34m prefix)
        if _RE_V8_COMMENT.match(line):
            continue
        # Pass through everything else (plain asm, labels, directives)
        cleaned.append(line)

    if v8_format:
        # V8 print-code uses hybrid syntax: AT&T suffixes + Intel operands.
        # Strip REX.W prefixes, size suffixes, and trailing annotations.
        fixed: list[str] = []
        for line in cleaned:
            line = _RE_TRAILING_ANNOTATION.sub("", line)
            line = _RE_REX_PREFIX.sub("", line)
            line = _RE_SIZE_SUFFIX.sub(r"\1\2", line)
            if line.strip():
                fixed.append(line)
        cleaned = fixed

    # Convert absolute jump/call targets to labels (both formats).
    label_map: dict[str, str] = {}
    fixed = []
    for line in cleaned:
        m = _RE_ABS_JUMP.match(line.strip())
        if m:
            addr = m.group(2)
            if addr not in label_map:
                label_map[addr] = f".L{len(label_map)}"
            line = f"{m.group(1)} {label_map[addr]}"
        fixed.append(line)

    return "\n".join(fixed)


@mcp.tool()
def llvm_mca(
    assembly: str,
    arch: str = "x64",
    cpu: str | None = None,
    syntax: str = "intel",
    bottleneck: bool = True,
    timeline: bool = False,
) -> CallToolResult:
    """Run llvm-mca pipeline analysis on raw assembly (e.g. from perf_annotate).

    Simulates how the CPU pipeline would execute the given instructions and
    reports throughput, latency, bottlenecks, and port pressure.

    assembly:    assembly text (from V8 JIT / perf / GDB disassemble)
    arch:        target architecture — "x64" or "arm64"
    cpu:         CPU model for scheduling simulation.
                 x64: skylake, znver3, alderlake, znver4, ...
                 arm64: neoverse-n1, neoverse-v2, cortex-a76, cortex-x2, ...
    syntax:      x64 only — "intel" (default) or "att"; auto-detected from
                 GDB/perf output. Ignored for arm64.
    bottleneck:  include bottleneck analysis showing what limits throughput
    timeline:    include cycle-by-cycle pipeline timeline (verbose)
    """
    mca = shutil.which("llvm-mca")
    if mca is None:
        return _text_result(
            "Error: llvm-mca not found. Install LLVM (e.g. pacman -S llvm)."
        )

    is_arm64 = arch.lower() in ("arm64", "aarch64")

    src = _clean_asm_for_mca(assembly.strip())

    if is_arm64:
        att = False
    else:
        att = syntax.lower() == "att"
        # Auto-detect AT&T syntax from % register prefixes (e.g. GDB default output)
        if not att and _re.search(
            r"%[re]?[abcd]x|%[re]?[sd]i|%[re]?[bs]p|%r\d+|%xmm", src
        ):
            att = True
        # Prepend syntax directive if not already present
        if ".intel_syntax" not in src and ".att_syntax" not in src:
            if att:
                src = ".att_syntax\n" + src
            else:
                src = ".intel_syntax noprefix\n" + src

    cmd = [
        mca,
        "--noalias",
        "--skip-unsupported-instructions=any",
    ]
    if is_arm64:
        cmd += ["-march=aarch64", "-mtriple=aarch64-linux-gnu"]
    else:
        # output-asm-variant: 0=AT&T, 1=Intel
        cmd.append(f"--output-asm-variant={'0' if att else '1'}")
    if cpu:
        cmd.append(f"--mcpu={cpu}")
    if bottleneck:
        cmd.append("--bottleneck-analysis")
    if timeline:
        cmd.append("--timeline")

    r = subprocess.run(cmd, input=src, capture_output=True, text=True, timeout=30)

    lines: list[str] = []
    header = f"# llvm-mca{f' -mcpu={cpu}' if cpu else ''}"
    lines.append(header)

    if r.stderr.strip():
        for line in r.stderr.strip().splitlines():
            if (
                "found a return instruction" in line
                or "program counter updates" in line
            ):
                continue
            lines.append(line)

    if r.returncode != 0 and not r.stdout.strip():
        lines.append(f"llvm-mca exited with code {r.returncode}")
        return _text_result("\n".join(lines))

    if r.stdout.strip():
        lines.append(_filter_mca_output(r.stdout))

    return _text_result("\n".join(lines))


def _filter_mca_output(raw: str) -> str:
    """Filter llvm-mca output to keep only the most useful sections.

    Always keeps: summary, bottleneck analysis, critical sequence,
    instruction info. Only includes resource pressure tables when the
    bottleneck analysis indicates resource pressure is significant (>10%).
    """
    sections: list[tuple[str, list[str]]] = []
    current_name = "summary"
    current_lines: list[str] = []

    # Known section headers
    _SECTION_STARTS = {
        "Cycles with backend pressure": "bottleneck",
        "Critical sequence": "critical",
        "Instruction Info": "instruction_info",
        "Resources:": "resources",
        "Resource pressure per iteration": "pressure_summary",
        "Resource pressure by instruction": "pressure_detail",
        "Timeline view": "timeline",
        "Average Wait times": "wait_times",
    }

    for line in raw.strip().splitlines():
        for prefix, name in _SECTION_STARTS.items():
            if line.startswith(prefix):
                sections.append((current_name, current_lines))
                current_name = name
                current_lines = []
                break
        current_lines.append(line)
    sections.append((current_name, current_lines))

    # Check if resource pressure is a significant bottleneck
    resource_pressure_pct = 0.0
    for name, slines in sections:
        if name == "bottleneck":
            for sl in slines:
                if "Resource Pressure" in sl and "%" in sl:
                    try:
                        resource_pressure_pct = float(
                            sl.split("[")[1].split("%")[0].strip()
                        )
                    except (IndexError, ValueError):
                        pass
                    break

    keep = {
        "summary",
        "bottleneck",
        "critical",
        "instruction_info",
        "timeline",
        "wait_times",
    }
    if resource_pressure_pct > 10:
        keep.update({"resources", "pressure_summary", "pressure_detail"})

    out: list[str] = []
    for name, slines in sections:
        if name in keep:
            # Strip excessive blank lines
            text = "\n".join(slines).strip()
            if text:
                out.append(text)

    return "\n\n".join(out)


# ---------------------------------------------------------------------------
# Godbolt (Compiler Explorer)
# ---------------------------------------------------------------------------

_godbolt_compiler_cache: dict[str, list[dict]] | None = None

_GODBOLT_ISET_MAP = {
    "x64": {"amd64", "x86-64", "x86_64"},
    "arm64": {"aarch64", "arm64"},
}


def _godbolt_get_compilers(language: str) -> list[dict]:
    """Fetch and cache compiler list from Godbolt. Cached per-language for process lifetime."""
    import httpx

    global _godbolt_compiler_cache
    if _godbolt_compiler_cache is None:
        _godbolt_compiler_cache = {}
    if language not in _godbolt_compiler_cache:
        r = httpx.get(
            f"https://godbolt.org/api/compilers/{language}",
            params={"fields": "id,name,semver,instructionSet"},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        _godbolt_compiler_cache[language] = r.json()
    return _godbolt_compiler_cache[language]


def _godbolt_resolve_compiler(compiler: str, target: str, language: str) -> str:
    """Resolve (compiler, target) to a Godbolt compiler ID."""
    compilers = _godbolt_get_compilers(language)
    iset_aliases = _GODBOLT_ISET_MAP.get(target, {target.lower()})

    matches = []
    for c in compilers:
        iset = (c.get("instructionSet") or "").lower()
        if iset not in iset_aliases:
            continue
        name = (c.get("name") or "").lower()
        if compiler.lower() not in name:
            continue
        matches.append(c)

    if not matches:
        raise ValueError(
            f"No Godbolt compiler found for compiler={compiler!r}, "
            f"target={target!r}, language={language!r}"
        )

    def sort_key(c: dict) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in (c.get("semver") or "0").split("."))
        except ValueError:
            return (0,)

    matches.sort(key=sort_key, reverse=True)
    return matches[0]["id"]


_MCA_DEFAULT_CPU = {"x64": "skylake", "arm64": "cortex-a76"}


@mcp.tool()
def godbolt_compile(
    source: str,
    target: str = "x64",
    compiler: str = "clang",
    language: str = "c++",
    flags: str = "-O3 -fno-strict-aliasing -fno-omit-frame-pointer",
    compiler_id: str | None = None,
    mca: bool = False,
    opt_remarks: bool = False,
) -> CallToolResult:
    """Compile a code snippet on Godbolt and return the assembly output.

    source:      the source code to compile
    target:      "x64" or "arm64" (default: "x64")
    compiler:    "gcc" or "clang" (default: "clang")
    language:    "c++" or "c" (default: "c++")
    flags:       compiler flags (default: V8 release flags)
    compiler_id: exact Godbolt compiler ID; overrides target/compiler/language
    mca:         run llvm-mca pipeline analysis on the assembly (clang only).
                 Shows throughput, bottlenecks, and port pressure per instruction.
                 Useful to check if a loop is bound by execution ports or latency.
    opt_remarks: include LLVM optimization pass remarks (clang only).
                 Shows which optimizations fired or failed and why.
                 Useful to understand missed inlining, vectorization, etc.
    """
    import httpx

    if (
        (mca or opt_remarks)
        and "clang" not in compiler.lower()
        and (compiler_id is None or "clang" not in compiler_id.lower())
    ):
        return _text_result("Error: mca and opt_remarks require a Clang compiler.")

    if compiler_id is None:
        compiler_id = _godbolt_resolve_compiler(compiler, target, language)

    options: dict = {
        "userArguments": flags,
        "filters": {
            "intel": True,
            "demangle": True,
            "commentOnly": True,
            "directives": True,
        },
    }

    if mca:
        cpu = _MCA_DEFAULT_CPU.get(target, "")
        mca_arg = f"-mcpu={cpu}" if cpu else ""
        options["tools"] = [{"id": "llvm-mcatrunk", "args": mca_arg}]

    if opt_remarks:
        options["compilerOptions"] = {"produceOptInfo": True}

    r = httpx.post(
        f"https://godbolt.org/api/compiler/{compiler_id}/compile",
        json={"source": source, "lang": language, "options": options},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    lines: list[str] = [f"# {compiler_id} {flags}"]

    stderr_lines = data.get("stderr") or []
    if stderr_lines:
        for s in stderr_lines:
            lines.append(s.get("text", ""))
        lines.append("")

    asm_lines = data.get("asm") or []
    for a in asm_lines:
        lines.append(a.get("text", ""))

    if mca:
        for tool_entry in data.get("tools") or []:
            if tool_entry.get("id") == "llvm-mcatrunk":
                lines.append("")
                lines.append("# --- llvm-mca analysis ---")
                for s in tool_entry.get("stderr") or []:
                    lines.append(s.get("text", ""))
                for s in tool_entry.get("stdout") or []:
                    lines.append(s.get("text", ""))

    if opt_remarks:
        opt_output = data.get("optOutput") or []
        if opt_output:
            lines.append("")
            lines.append("# --- optimization remarks ---")
            for opt_type in ("Missed", "Passed", "Analysis"):
                entries = [o for o in opt_output if o.get("optType") == opt_type]
                if not entries:
                    continue
                lines.append(f"# {opt_type} ({len(entries)}):")
                for o in entries:
                    loc = o.get("DebugLoc") or {}
                    loc_str = (
                        f"{loc.get('File', '?')}:{loc.get('Line', '?')}" if loc else ""
                    )
                    fn = o.get("Function", "")
                    display = o.get("displayString", "")
                    lines.append(f"  [{fn}] {loc_str}: {display}")

    return _text_result("\n".join(lines))


@mcp.tool()
def godbolt_list_compilers(
    language: str = "c++",
    filter: str | None = None,
) -> CallToolResult:
    """List available compilers on Godbolt for a language. Use filter to narrow results.

    language: "c++", "c", "rust", etc. (default: "c++")
    filter:   substring match on name/instructionSet, e.g. "clang 19" or "arm64"
    """
    compilers = _godbolt_get_compilers(language)

    if filter:
        needle = filter.lower()
        compilers = [
            c
            for c in compilers
            if needle in (c.get("name") or "").lower()
            or needle in (c.get("instructionSet") or "").lower()
        ]

    lines = [f"{'id':<30} {'name':<45} {'instructionSet'}"]
    lines.append("-" * len(lines[0]))
    for c in compilers:
        lines.append(
            f"{c.get('id', ''):<30} {c.get('name', ''):<45} {c.get('instructionSet', '')}"
        )

    if len(lines) == 2:
        return _text_result("No compilers matched the filter.")

    return _text_result("\n".join(lines))


# ── Worktree management ─────────────────────────────────────────────────────


@mcp.tool()
def worktree(
    action: str,
    name: str | None = None,
    branch: str | None = None,
    upstream: str = "main",
) -> CallToolResult:
    """Manage V8 git worktrees with automatic gclient dependency symlinking.

    Worktrees are created as siblings of the main V8 checkout (e.g. name="foo"
    creates ~/src/v8/foo). gclient-managed dependencies (build/, buildtools/,
    third_party/*, etc.) are symlinked from the main checkout — no gclient sync
    needed. To update shared deps, run gclient sync in the main checkout.

    action:   "create", "remove", or "list"
    name:     worktree directory name (required for create/remove)
    branch:   branch to check out (create only, optional).
              If it exists, checks it out. Otherwise creates a new branch.
              Defaults to the worktree name.
    upstream: base branch/ref for the new branch (default "main")
    """
    repo = _resolve_repo("v8")

    if action == "list":
        wts = worktree_mod.list_worktrees(repo)
        if not wts:
            return _text_result("No worktrees found.")
        lines = [f"{'path':<50} {'branch':<30} {'head'}"]
        lines.append("-" * len(lines[0]))
        for wt in wts:
            lines.append(
                f"{wt['path']:<50} {wt.get('branch', ''):<30} {wt.get('head', '')}"
            )
        return _text_result("\n".join(lines))

    if not name:
        raise ValueError(f"'name' is required for action={action!r}")

    if action == "create":
        wt_path = worktree_mod.create(repo, name, branch, upstream=upstream)
        return _text_result(
            f"Worktree created at {wt_path}\n"
            f"\n"
            f"All commands must use this absolute path, e.g.:\n"
            f"  git -C {wt_path} status\n"
            f"\n"
            f"Build directories are NOT shared — use gm.py to set them up:\n"
            f"  cd {wt_path} && python3 tools/dev/gm.py x64.release.d8\n"
        )

    if action == "remove":
        worktree_mod.remove(repo, name)
        return _text_result(f"Worktree '{name}' removed.")

    raise ValueError(f"Unknown action {action!r}. Use 'create', 'remove', or 'list'.")
