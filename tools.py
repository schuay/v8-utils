"""MCP tool definitions for v8-utils."""

from mcp.server.fastmcp import FastMCP

import config
import daemon
import gerrit as gerrit_tools
import jsb as jsb_module
import perf as perf_tools
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
def pinpoint_show_results(
    job_url: str,
    show_all: bool = False,
    use_cas: bool = False,
) -> str:
    """Show a base-vs-experiment comparison table for a Pinpoint job.

    One row per metric: base mean±stdev, exp mean±stdev, %change, p-value,
    significance marker. Sorted by %change descending.

    show_all: if False (default), only show statistically significant results.
    job_url:  Pinpoint job URL or job ID
    use_cas:  if True, fetch raw per-run values from CAS isolates instead of
              the histogram HTML. Slower but surfaces richer sub-metrics for
              JetStream (Score, First, Average, Worst4 per story).
              Requires: gcloud auth application-default login
    """
    job_id = pinpoint.job_id_from_url(job_url)
    all_rows = pinpoint.pivot_results_cas(job_id) if use_cas else pinpoint.pivot_results(job_id)
    if not all_rows:
        return "No results found."

    rows = all_rows if show_all else [r for r in all_rows if r["significant"]]
    omitted = len(all_rows) - len(rows)
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

    def fmt_unit(raw: str) -> str:
        if raw.endswith("_biggerIsBetter"):
            return raw[:-len("_biggerIsBetter")] + " (bigger is better)"
        if raw.endswith("_smallerIsBetter"):
            return raw[:-len("_smallerIsBetter")] + " (smaller is better)"
        return raw

    units = sorted({r["unit"] for r in rows if r.get("unit")})
    unit_line = "unit: " + ",  ".join(fmt_unit(u) for u in units)

    sep = "-" * (sum(widths) + 2 * (len(widths) - 1))
    lines = [
        f"base: {rows[0]['base_label']}",
        f"exp:  {rows[0]['exp_label']}",
        unit_line,
        "",
        fmt_row(hdrs),
        sep,
        *[fmt_row(c) for c in cells],
    ]
    if omitted:
        lines.append(f"({omitted} non-significant result{'s' if omitted != 1 else ''} omitted)")
    return "\n".join(lines)


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
    server = _git("config", f"branch.{branch}.gerritserver") \
             or "https://chromium-review.googlesource.com"
    patchset = _git("config", f"branch.{branch}.gerritpatchset")
    url = f"{server}/{issue}"
    return f"{url}/{patchset}" if patchset else url


def chat_notify_watching(job_url: str) -> None:
    """Send a 'Watching' notification to Google Chat if configured."""
    cfg = config.load()
    if cfg.chat_app_space and cfg.chat_service_account_email:
        try:
            import chat
            chat.notify(cfg.chat_app_space, cfg.chat_service_account_email,
                        f"👀 Watching: {job_url}")
        except Exception:
            pass
    elif cfg.chat_webhook:
        try:
            import httpx
            httpx.post(cfg.chat_webhook, json={"text": f"👀 Watching: {job_url}"}, timeout=10)
        except Exception:
            pass


def _job_url(job: dict) -> str | None:
    """Extract the Pinpoint job URL from a job detail dict."""
    jid = job.get("job_id")
    if not jid:
        return None
    return job.get("url") or f"https://pinpoint-dot-chromeperf.appspot.com/job/{jid}"


def create_pinpoint_jobs(
    benchmarks: list[str],
    configurations: list[str],
    *,
    story: str | None = None,
    story_tags: str | None = None,
    base_git_hash: str | None = None,
    exp_git_hash: str | None = None,
    base_patch: str | None = None,
    exp_patches: list[str | None] | None = None,
    base_js_flags: str | None = None,
    exp_js_flags_list: list[str | None] | None = None,
    repeat: int = 100,
    bug_id: int | None = None,
    on_auto_hash: callable = None,
    on_job_created: callable = None,
    on_watching: callable = None,
    watch: bool | None = None,
) -> list[dict]:
    """Shared core for creating Pinpoint A/B jobs.

    Creates one job per combination of configuration × benchmark × exp_patch × exp_js_flags.

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

    # Auto-detect exp-patch from current git branch when not provided
    if exp_patches is None:
        detected = get_gerrit_issue_url()
        exp_patches = [detected]  # None is valid (job with no patch)

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

    combos = list(itertools.product(configurations, pairs, exp_patches, exp_js_flags_list))
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
            job_detail = pinpoint_show_job(job_url)
            jobs.append(job_detail)
        else:
            jobs.append(result)
        if on_job_created:
            on_job_created(i, len(combos),
                           (cfg, bench, default_story, exp_patch, exp_js_flags),
                           jobs[-1])

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


@mcp.tool()
def pinpoint_create_job(
    benchmark: str = "js3 sp3",
    configuration: str = "m1",
    story: str | None = None,
    story_tags: str | None = None,
    base_git_hash: str | None = None,
    exp_git_hash: str | None = None,
    base_patch: str | None = None,
    exp_patch: str | None = None,
    base_js_flags: str | None = None,
    exp_js_flags: str | None = None,
    repeat: int = 100,
    bug_id: int | None = None,
) -> dict:
    """Create a new Pinpoint A/B try job. Requires luci-auth login.

    Creates jobs for each combination of benchmark × configuration.
    When chat integration is configured, created jobs are automatically
    watched and a notification is sent on completion.

    benchmark:      space-separated benchmark names or aliases (default: "js3 sp3"):
                      "js3"  → jetstream-main.crossbench (story: JetStream)
                      "js2"  → jetstream2.crossbench     (story: JetStream2)
                      "sp3"  → speedometer3.crossbench   (story: Speedometer3)
    configuration:  space-separated bot config(s) or alias(es) (default: "m1"):
                      "linux" → linux-r350-perf
                      "m1"    → mac-m1_mini_2020-perf
                      "m4"    → mac-m4-mini-perf
    story:          story within the benchmark (overrides alias default)
    story_tags:     comma-separated story tags to select stories
    base_git_hash:  git hash for the base build (default: latest cached CI build)
    exp_git_hash:   git hash for the experiment build (default: latest cached CI build)
    base_patch:     Gerrit patch for base — change ID, crrev/c/12345, or full URL
    exp_patch:      Gerrit patch for experiment — same formats
    base_js_flags:  V8 flags for base, passed as --js-flags="...", e.g. "--turbofan"
    exp_js_flags:   V8 flags for experiment, same format
    repeat:         number of bot runs per variant (default: 100)
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
        exp_patches=[exp_patch],
        base_js_flags=base_js_flags,
        exp_js_flags_list=[exp_js_flags],
        repeat=repeat,
        bug_id=bug_id,
    )
    return {"jobs": jobs} if len(jobs) != 1 else jobs[0]


# ── jsb tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def jsb_run_bench(
    bench: str,
    builds: list[str],
    runs: int = 5,
    suite: str = "js3",
) -> dict:
    """Run a JetStream2/3 story with one or more d8 builds and return scores.

    bench:  benchmark story name, e.g. "regexp-octane", "chai-wtb"
    builds: list of "build[:flags]" specs under v8_out, e.g.:
              ["release-main", "release-lto:--turbolev-future"]
    runs:   number of runs per variant (default: 5)
    suite:  "js2" or "js3" (default: "js3")

    Returns per-variant scores with mean, stdev, stdev_pct, and a
    formatted comparison table when multiple variants are given.

    Configure paths in ~/.config/v8-utils/config.toml:
      v8_out        = "~/v8/out"
      js2_dir       = "~/JetStream2"
      js3_dir       = "~/JetStream3"
      default_build = "release"
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

    results = [
        jsb_module.run_variant(v, suite_dir, bench, runs, js3, cfg.v8_out)
        for v in variants
    ]

    return {
        "bench": bench,
        "suite": suite_label,
        "runs": runs,
        "variants": [
            {"label": v.label, "scores": s}
            for v, s in zip(variants, jsb_module.summarise(results))
        ],
        "table": jsb_module.format_table(bench, suite_label, runs, variants, results),
    }


# ── gerrit tools ─────────────────────────────────────────────────────────────

@mcp.tool()
def gerrit_comments(change_url: str) -> list[dict]:
    """Fetch all published comments on a Gerrit CL, threaded by file and line.

    Each entry represents a comment thread and includes:
      file, line, patch_set, author, message, updated, replies[]

    Threads are sorted by file path then line number.  Use this to understand
    reviewer feedback or the current state of a code review.

    change_url: Gerrit CL URL, e.g.:
      https://chromium-review.googlesource.com/c/v8/v8/+/7650974
      https://chromium-review.googlesource.com/7650974
    """
    return gerrit_tools.comments(change_url)


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


# ── perf tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def perf_stat(stat_file: str) -> dict:
    """Parse a saved `perf stat` output file into structured counter data.

    stat_file: path to a file containing `perf stat` text output
               (saved via `perf stat -o <file>` or stderr redirection)

    Returns elapsed_seconds and a list of counters with their values and
    human-readable notes (e.g. "3.45 CPUs utilized").
    """
    return perf_tools.parse_stat(stat_file)


@mcp.tool()
def perf_hotspots(
    perf_data: str,
    dso: str | None = None,
    n: int = 30,
) -> list[dict]:
    """Return the top N hot symbols from a perf.data file.

    Each entry includes self_pct (exclusive time) and total_pct (inclusive
    time including callees), plus the symbol name and shared object.
    Sorted by self_pct descending — start here to find what to annotate.

    perf_data: path to perf.data file
    dso:       restrict to a specific shared object, e.g. "libv8.so" or "d8"
    n:         number of symbols to return (default 30)
    """
    return perf_tools.hotspots(perf_data, dso=dso, n=n)


@mcp.tool()
def perf_callers(
    perf_data: str,
    symbol: str,
    n: int = 20,
) -> str:
    """Show who calls a hot symbol and with what sample weight.

    Returns the call-graph section for the symbol from perf report in
    caller mode, so the tree reads upward (direct callers nearest, then
    their callers above).  Use this to understand whether hotness is
    self-time or propagated from a call site.

    perf_data: path to perf.data file
    symbol:    symbol name or unique substring, e.g. "Heap::AllocateRaw"
    n:         max lines of call-graph detail to return (default 20)
    """
    return perf_tools.callers(perf_data, symbol, n=n)


@mcp.tool()
def perf_annotate(
    perf_data: str,
    symbol: str,
    dso: str | None = None,
    min_pct: float = 0.5,
    context: int = 8,
) -> dict:
    """Annotated disassembly for a symbol, with smart hot-region extraction.

    Returns:
      total_lines:      total line count — use as reference for read_around
      top_instructions: the 20 hottest individual instructions (addr, pct, asm)
      hot_blocks:       contiguous clusters of hot instructions (>= min_pct),
                        each expanded by ±context lines and sorted by peak heat

    The hot_blocks content includes line numbers so you can call
    perf_annotate_read_around to explore the surrounding code.

    perf_data: path to perf.data file
    symbol:    exact symbol name (use perf_hotspots to find it)
    dso:       shared object filter, e.g. "libv8.so"
    min_pct:   minimum sample % to qualify as hot (default 0.5)
    context:   lines of context around each hot cluster (default 8)
    """
    return perf_tools.annotate(perf_data, symbol, dso=dso,
                               min_pct=min_pct, context=context)


@mcp.tool()
def perf_annotate_read_around(
    perf_data: str,
    symbol: str,
    line: int,
    context: int = 30,
    dso: str | None = None,
) -> str:
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
    return perf_tools.annotate_read_around(perf_data, symbol, line,
                                           context=context, dso=dso)


@mcp.tool()
def perf_flamegraph(
    perf_data: str,
    focus_symbol: str | None = None,
    dso: str | None = None,
    min_pct: float = 0.5,
    depth: int = 8,
) -> str:
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
    return perf_tools.flamegraph(perf_data, focus_symbol=focus_symbol,
                                 dso=dso, min_pct=min_pct, depth=depth)


@mcp.tool()
def perf_tma(
    perf_data: str,
    symbol: str | None = None,
    n: int = 20,
) -> dict:
    """Microarchitecture bottleneck analysis (TMA Level 1) per symbol.

    Always safe to call — returns {available: false, message: ...} when the
    perf.data was not recorded with TMA events, so callers can handle both
    cases without branching.

    Uses real Skylake-SP kernel PMU topdown events (topdown-fetch-bubbles,
    topdown-slots-issued/retired, topdown-recovery-bubbles, topdown-total-slots)
    rather than the unreliable stalled-cycles-frontend/-backend aliases.

    Intensity fields = event_pct / cycles_pct for each symbol:
      ~1.0  proportional to cycle share (average)
      >1.0  disproportionately high — likely bottleneck
      <1.0  below average

    When available=true, each symbol entry includes:
      cycles_pct:          share of cycle samples (hotness)
      fe_intensity:        Frontend Bound  (fetch bubbles / cycles)
      retiring_intensity:  Retiring        (slots retired / cycles; high = efficient)
      bad_spec_intensity:  Bad Speculation (issued - retired slots / cycles)
      mem_intensity:       Backend→Memory  (L3-stall cycles / cycles)
                           only present when recorded with --topdown
      dominant:            "Frontend Bound" | "Backend Bound (Memory)" |
                           "Backend Bound (Core)" | "Bad Speculation" |
                           "Retiring (efficient)" | "Mixed"
      has_mem_detail:      true if mem_intensity is populated

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
    return perf_tools.tma(perf_data, symbol=symbol, n=n)


@mcp.tool()
def perf_diff(
    perf_before: str,
    perf_after: str,
    dso: str | None = None,
    n: int = 30,
) -> list[dict]:
    """Compare two perf profiles: what got hotter or cooler?

    Returns the top N symbols sorted by |delta_pct|, so the biggest
    changes appear first regardless of direction.  Each entry has:
      symbol:       function name
      dso:          shared object
      baseline_pct: self% in the before profile (None if new)
      after_pct:    self% in the after profile (None if removed)
      delta_pct:    after - baseline (positive = got hotter)

    perf_before: path to baseline perf.data
    perf_after:  path to experiment perf.data
    dso:         restrict to a specific shared object
    n:           number of symbols to return (default 30)
    """
    return perf_tools.diff(perf_before, perf_after, dso=dso, n=n)
