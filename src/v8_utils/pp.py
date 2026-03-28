"""pp — Pinpoint CLI wrapper around the v8-utils tool functions.

Usage:
  pp show-job <job_url> [<job_url> ...]
  pp list-jobs [--patch CL] [--status S] [--benchmark B] [--bot BOT] [--filter KEY=VALUE]
  pp show-results <job_url> [<job_url> ...] [--show-all]
  pp create-job -t TEMPLATE [TEMPLATE ...] -c CONFIG [CONFIG ...] [options]
  pp watch <job_url> [<job_url> ...]
  pp daemon-stop
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
from datetime import datetime

from . import chat
from . import config
from . import daemon
from . import pinpoint

from .tools import (
    _fetch_job_details_sorted,
    _fetch_jobs_list,
    _format_results_table,
    _run_concurrent,
    chat_notify_watching,
    create_pinpoint_jobs,
    resolve_exp_patches,
    resolve_patch_filter,
)

# ── ANSI colors (no-ops when not a TTY) ───────────────────────────────────────

if sys.stdout.isatty():
    _BOLD = "\033[1m"
    _DIM = "\033[2m"
    _RED = "\033[31m"
    _GREEN = "\033[32m"
    _YELLOW = "\033[33m"
    _CYAN = "\033[36m"
    _RESET = "\033[0m"
else:
    _BOLD = _DIM = _RED = _GREEN = _YELLOW = _CYAN = _RESET = ""


def _status_color(status: str) -> str:
    s = status.lower()
    if "complet" in s:
        return f"{_GREEN}{status}{_RESET}"
    if any(x in s for x in ("running", "queue", "pending", "schedul")):
        return f"{_YELLOW}{status}{_RESET}"
    if any(x in s for x in ("fail", "cancel", "error")):
        return f"{_RED}{status}{_RESET}"
    return status


_JSON_RE = re.compile(
    r'("(?:[^"\\]|\\.)*")\s*:'  # key
    r'|("(?:[^"\\]|\\.)*")'  # string value
    r"|(true|false|null)"  # boolean / null
    r"|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"  # number
)


def _colorize_json(text: str) -> str:
    if not _CYAN:
        return text

    def _replace(m: re.Match) -> str:
        if m.group(1):  # key
            return f"{_CYAN}{m.group(1)}{_RESET}:"
        if m.group(2):  # string value
            return f"{_GREEN}{m.group(2)}{_RESET}"
        if m.group(3):  # bool / null
            return f"{_YELLOW}{m.group(3)}{_RESET}"
        if m.group(4):  # number
            return f"{_YELLOW}{m.group(4)}{_RESET}"
        return m.group(0)

    return _JSON_RE.sub(_replace, text)


# ── Output helpers ─────────────────────────────────────────────────────────────


def _out(result) -> None:
    if isinstance(result, str):
        print(result)
    else:
        print(_colorize_json(json.dumps(result, indent=2)))


def _make_progress():
    """Create a rich Progress instance on stderr, or None if not a TTY."""
    if not sys.stderr.isatty():
        return None
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    return Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(bar_width=20),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=Console(stderr=True),
        transient=True,
    )


def _fetch_label(user: str, since) -> str:
    """Build a progress label like 'Fetching jobs (user, since Mar 01)'."""
    parts = [user.split("@")[0]]
    if since and since != datetime.min:
        parts.append(f"since {since.strftime('%b %d')}")
    return f"Fetching jobs ({', '.join(parts)})"


@contextlib.contextmanager
def _progress_ctx(label: str, total: int | None = None):
    """Single-task progress context. Yields an on_progress callback."""
    progress = _make_progress()
    if progress is None:
        yield None
        return
    with progress:
        task = progress.add_task(label, total=total)

        def on_progress(done: int, total: int) -> None:
            progress.update(task, completed=done, total=total)

        yield on_progress


# ── Command handlers ───────────────────────────────────────────────────────────


def _print_job(j: dict) -> None:
    url = f"{_CYAN}https://pinpoint-dot-chromeperf.appspot.com/job/{j.get('job_id')}{_RESET}"
    created = (j.get("created") or "")[:16].replace("T", " ")
    status = j.get("status") or "?"
    print(f"{_DIM}{created}{_RESET}  {_status_color(status)}  {url}")
    print()

    patch_url = j.get("experiment_patch")
    patch_subject = pinpoint.fetch_gerrit_subject(patch_url) if patch_url else None

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
        colored = []
        for part in header_parts:
            key, _, val = part.partition(": ")
            colored.append(f"{_DIM}{key}:{_RESET} {_BOLD}{val}{_RESET}")
        print(f" {_DIM}│{_RESET} ".join(colored))
        print()

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
        if key == "patch":
            subject_str = f'  {_BOLD}"{patch_subject}"{_RESET}' if patch_subject else ""
            val_str = f"{_CYAN}{val}{_RESET}{subject_str}"
        elif key == "results":
            val_str = f"{_CYAN}{val}{_RESET}"
        else:
            val_str = str(val)
        print(f"  {_DIM}{key:<{w}}{_RESET}  {val_str}")


def _cmd_show_job(args: argparse.Namespace) -> None:
    with _progress_ctx("Fetching jobs", total=len(args.job_urls)) as on_progress:
        paired = _fetch_job_details_sorted(args.job_urls, on_progress=on_progress)
    for i, (jid, detail) in enumerate(paired):
        if i:
            print(f"{_DIM}{'─' * 60}{_RESET}")
        if "error" in detail:
            print(f"Error fetching {jid}: {detail['error']}")
        else:
            _print_job(detail)


def _cmd_cancel_job(args: argparse.Namespace) -> None:
    reason = args.reason

    def cancel(url: str) -> str:
        try:
            result = pinpoint.cancel_job(url, reason=reason)
            job_id = result.get("job_id", pinpoint.job_id_from_url(url))
            state = result.get("state", "unknown")
            return f"Job {job_id}: {state}"
        except Exception as e:
            job_id = pinpoint.job_id_from_url(url)
            return f"Job {job_id}: Error: {e}"

    fns = [lambda u=u: cancel(u) for u in args.job_urls]
    with _progress_ctx("Cancelling", total=len(fns)) as on_progress:
        results = _run_concurrent(fns, on_progress)
    for line in results:
        print(line)


def _build_filters(
    args: argparse.Namespace, *, extra: list[str] | None = None
) -> list[str]:
    """Build job filter list from common CLI flags (--patch, --status, etc.)."""
    filters = list(extra or [])
    patch = resolve_patch_filter(args.patch)
    if args.patch and args.patch.lower() == "auto" and patch:
        print(f"{_DIM}autodetected --patch: {patch} (from current branch){_RESET}")
    if patch:
        filters.append(f"patch={patch}")
    if args.status:
        filters.append(f"status={args.status}")
    if args.benchmark:
        filters.append(f"benchmark={args.benchmark}")
    if args.bot:
        filters.append(f"bot={args.bot}")
    return filters


def _resolve_user(args: argparse.Namespace) -> str:
    """Resolve user email from --user flag, config, or luci-auth."""
    return args.user or config.load().user or pinpoint.get_current_user_email()


def _cmd_list_jobs(args: argparse.Namespace) -> None:
    filters = _build_filters(args)
    since = pinpoint.parse_since(args.since)
    user = _resolve_user(args)
    label = _fetch_label(user, since)
    progress = _make_progress()
    if progress:
        with progress:
            t1 = progress.add_task(label, total=None)
            jobs = _fetch_jobs_list(
                count=args.recent, user=user, filters=filters or None, since=since
            )
            progress.update(t1, total=1, completed=1)
            if not jobs:
                pass
            else:
                patches = [j.get("experiment_patch") or "" for j in jobs]
                fns = [
                    lambda p=p: pinpoint.fetch_gerrit_subject(p) if p else None
                    for p in patches
                ]
                t2 = progress.add_task("Fetching details", total=len(fns))
                subjects = _run_concurrent(
                    fns, lambda done, total: progress.update(t2, completed=done)
                )
    else:
        jobs = _fetch_jobs_list(
            count=args.recent, user=user, filters=filters or None, since=since
        )
        patches = [j.get("experiment_patch") or "" for j in jobs] if jobs else []
        fns = [
            lambda p=p: pinpoint.fetch_gerrit_subject(p) if p else None for p in patches
        ]
        subjects = _run_concurrent(fns) if fns else []

    if not jobs:
        print("No jobs found.")
        return
    # Display oldest first (API returns newest first).
    jobs.reverse()
    subjects = list(reversed(subjects))

    for j, subject in zip(jobs, subjects):
        created = (j.get("created") or "")[:16].replace("T", " ")
        status = j.get("status") or "?"
        url = j.get("url") or ""
        config_ = pinpoint.short_configuration(j.get("configuration") or "")
        benchmark = pinpoint.short_benchmark(j.get("benchmark") or "")
        story = j.get("story") or ""
        diff = j.get("difference_count")
        patch = j.get("experiment_patch") or ""
        base_flags = j.get("base_extra_args") or ""
        exp_flags = j.get("experiment_extra_args") or ""

        label = f"{benchmark} / {story}".strip(" /")
        diff_str = f"  {_YELLOW}diffs={diff}{_RESET}" if diff is not None else ""
        print(
            f"{_DIM}{created}{_RESET}  {_status_color(f'{status:<12}')}  {_CYAN}{url}{_RESET}"
        )
        print(f"  {_DIM}{config_}{_RESET}  {_BOLD}{label}{_RESET}{diff_str}")
        if patch:
            subject_str = f'  {_BOLD}"{subject}"{_RESET}' if subject else ""
            print(f"  {_DIM}patch:{_RESET}      {_CYAN}{patch}{_RESET}{subject_str}")
        if base_flags:
            print(f"  {_DIM}base-flags:{_RESET} {base_flags}")
        if exp_flags:
            print(f"  {_DIM}exp-flags:{_RESET}  {exp_flags}")
        print()

    job_ids = [j.get("job_id", "") for j in jobs]
    print(f"{_DIM}job ids:{_RESET} {' '.join(job_ids)}")


def _cmd_show_results(args: argparse.Namespace) -> None:
    job_urls = list(args.job_urls)

    filters = _build_filters(args, extra=["status=Completed"])
    has_filters = len(filters) > 1 or args.recent or args.since

    progress = _make_progress()

    if args.recent or has_filters:
        since_str = args.since or ("one month ago" if has_filters else None)
        since = pinpoint.parse_since(since_str) if since_str else None
        count = args.recent or 20
        user = _resolve_user(args)
        if progress:
            progress.start()
            t_list = progress.add_task(_fetch_label(user, since), total=None)
        jobs = _fetch_jobs_list(count=count, user=user, filters=filters, since=since)
        if progress:
            progress.update(t_list, total=1, completed=1)
        if not jobs:
            if progress:
                progress.stop()
            print("No completed jobs found matching filters.")
            return
        job_urls.extend(j["job_id"] for j in jobs)

    if not job_urls:
        if progress:
            progress.stop()
        print("No jobs specified. Use job URLs/IDs, --recent N, or filter flags.")
        return

    ids = [pinpoint.job_id_from_url(u) for u in job_urls]
    if progress and not progress.live.is_started:
        progress.start()
    if progress:
        t_details = progress.add_task("Fetching details", total=len(ids))
    paired = _fetch_job_details_sorted(
        ids,
        on_progress=(
            (lambda d, t: progress.update(t_details, completed=d)) if progress else None
        ),
    )
    job_ids = [jid for jid, _ in paired]
    detail_map = dict(paired)

    use_ansi = bool(_CYAN)
    fns = [
        lambda jid=jid: _format_results_table(
            jid,
            args.show_all,
            args.use_cas,
            args.compact,
            job=detail_map.get(jid),
            ansi=use_ansi,
        )
        for jid in job_ids
    ]
    if progress:
        t_results = progress.add_task("Fetching results", total=len(fns))
    tables = _run_concurrent(
        fns,
        (lambda d, t: progress.update(t_results, completed=d)) if progress else None,
    )
    if progress:
        progress.stop()

    multi = len(job_ids) > 1
    for i, (job_id, table) in enumerate(zip(job_ids, tables)):
        if i:
            print(f"{_DIM}{'─' * 60}{_RESET}")
        if multi:
            print(
                f"{_DIM}──{_RESET} {_CYAN}https://pinpoint-dot-chromeperf.appspot.com/job/{job_id}{_RESET}"
            )
        if table is None:
            print("No results found.")
        else:
            print(table)


def _cmd_create_job(args: argparse.Namespace) -> None:
    # Resolve benchmark list from template or explicit --benchmark
    if args.benchmark:
        benchmarks = [args.benchmark]
    else:
        for t in args.template:
            if t not in pinpoint.BENCHMARK_ALIASES:
                known = ", ".join(pinpoint.BENCHMARK_ALIASES)
                raise ValueError(f"Unknown template {t!r}. Known: {known}")
        benchmarks = list(args.template)

    # Resolve exp-patch: explicit values, or default to "auto" (detect from branch)
    raw_patches = args.exp_patch or ["auto"]
    exp_patches = resolve_exp_patches(raw_patches)
    for raw, resolved in zip(raw_patches, exp_patches):
        if raw.lower() == "auto" and resolved:
            print(
                f"{_DIM}autodetected exp-patch: {resolved} (from current branch){_RESET}"
            )

    def on_auto_hash(cfg, commit, build_num_or_err):
        if commit:
            print(
                f"{_DIM}using latest build: {commit[:12]} ({cfg}, build #{build_num_or_err}){_RESET}"
            )
        else:
            print(
                f"{_YELLOW}warning: could not fetch latest build for {cfg}: {build_num_or_err}{_RESET}"
            )

    created_job_ids: list[str] = []

    def on_job_created(index, total, combo, job):
        cfg_name, bench, story, exp_patch, exp_js_flags = combo
        if total > 1 and index:
            print(f"{_DIM}{'─' * 60}{_RESET}")
        if total > 1:
            parts = [cfg_name, bench]
            if story:
                parts.append(story)
            if exp_patch:
                parts.append(exp_patch)
            if exp_js_flags:
                parts.append(f"flags:{exp_js_flags}")
            print(f"{_DIM}[{index + 1}/{total}] {' / '.join(parts)}{_RESET}")
        if job.get("job_id"):
            created_job_ids.append(job["job_id"])
            _print_job(job)
        else:
            _out(job)

    def on_watching(url):
        print(
            f"{_GREEN}Watching{_RESET} {url.split('/')[-1]} — you'll be notified on completion."
        )

    create_pinpoint_jobs(
        benchmarks=benchmarks,
        configurations=args.configuration,
        story=args.story,
        story_tags=args.story_tags,
        base_git_hash=args.base_git_hash,
        exp_git_hash=args.exp_git_hash,
        base_patch=args.base_patch,
        exp_patches=exp_patches,
        base_js_flags=args.base_js_flags,
        exp_js_flags_list=args.exp_js_flags or [None],
        repeat=args.repeat,
        bug_id=args.bug_id,
        on_auto_hash=on_auto_hash,
        on_job_created=on_job_created,
        on_watching=on_watching,
        watch=args.watch,
    )

    if created_job_ids:
        ids_str = " ".join(created_job_ids)
        print(f"\n{_DIM}Once jobs are done, show results using:{_RESET}")
        print(f"{_DIM}pp show-results {ids_str}{_RESET}")


def _cmd_watch(args: argparse.Namespace) -> None:
    if not daemon.is_running():
        daemon.start_background()
    for job_url in args.job_urls:
        daemon.send_job(job_url)
        chat_notify_watching(job_url)
        job_id = job_url.split("/")[-1]
        print(f"{_GREEN}Watching{_RESET} {job_id} — you'll be notified on completion.")


def _cmd_daemon_stop(args: argparse.Namespace) -> None:
    import signal as sig

    if not daemon.is_running():
        print(f"{_YELLOW}Daemon is not running.{_RESET}")
        return
    pid = int(daemon.PID_PATH.read_text())
    os.kill(pid, sig.SIGTERM)
    print(f"{_GREEN}Stopped daemon{_RESET} (pid {pid}).")


def _cmd_chat_setup(args: argparse.Namespace) -> None:
    cfg = config.load()
    if not cfg.chat_service_account_email:
        print(
            f"{_RED}error:{_RESET} chat_service_account_email not set in {config.CONFIG_PATH}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Service account: {_CYAN}{cfg.chat_service_account_email}{_RESET}")
    print("Identifying you via Application Default Credentials...")
    user_id = chat.adc_user_id()
    print(f"  {_DIM}Google user ID:{_RESET} {user_id}")

    print("Finding DM space with the bot...")
    print(
        f'  {_DIM}(In Google Chat, search for "v8-utils-pinpoint" and send it a message first.){_RESET}'
    )
    space = chat.find_dm_space(cfg.chat_service_account_email, user_id)
    print(f"  {_DIM}space:{_RESET} {space}")

    config.update_chat_app_space(space)
    chat.notify(
        space,
        cfg.chat_service_account_email,
        "👋 v8-utils notifications are set up. You'll be notified here when your Pinpoint jobs complete.",
    )
    print(f"{_GREEN}Done.{_RESET} Written to {config.CONFIG_PATH}")
    if daemon.is_running():
        print(
            f"{_YELLOW}Note:{_RESET} restart the daemon so it picks up the new config:"
        )
        print(f"  pp daemon-stop && pp watch <job_url>")


def _cmd_config(args: argparse.Namespace) -> None:
    print(config.template())


def _cmd_upgrade(args: argparse.Namespace) -> None:
    cmd = [
        "uv",
        "tool",
        "install",
        "git+https://github.com/schuay/v8-utils.git",
        "--reinstall",
        "--index-url",
        "https://pypi.org/simple/",
    ]
    cmd_str = " ".join(cmd)
    print(f"Running `{cmd_str}`.")
    print("If this fails due to permissions, run the command manually.")
    print()
    os.execvp("uv", cmd)


def _cmd_logs(args: argparse.Namespace) -> None:
    log_path = daemon.LOG_PATH
    if not log_path.exists():
        print(f"No log file yet ({log_path})", file=sys.stderr)
        sys.exit(1)
    if args.follow:
        os.execlp("tail", "tail", "-f", str(log_path))
    else:
        print(log_path.read_text(), end="")


def main() -> None:
    import logging

    parser = argparse.ArgumentParser(prog="pp", description="Pinpoint CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # show-job
    p = sub.add_parser("show-job", help="Show details of a Pinpoint job")
    p.add_argument(
        "job_urls",
        nargs="+",
        metavar="job_url",
        help="Pinpoint job URL(s) or job ID(s)",
    )
    p.set_defaults(func=_cmd_show_job)

    # cancel-job
    p = sub.add_parser("cancel-job", help="Cancel one or more Pinpoint jobs")
    p.add_argument(
        "job_urls",
        nargs="+",
        metavar="job_url",
        help="Pinpoint job URL(s) or job ID(s)",
    )
    p.add_argument(
        "--reason",
        default="Cancelled",
        help='Cancellation reason (default: "Cancelled")',
    )
    p.set_defaults(func=_cmd_cancel_job)

    # list-jobs
    p = sub.add_parser(
        "list-jobs", aliases=["l"], help="List recent Pinpoint jobs for a user"
    )
    p.add_argument(
        "-n",
        "--recent",
        type=int,
        default=20,
        metavar="N",
        help="Number of most recent jobs to show (default: 20)",
    )
    p.add_argument(
        "-u",
        "--user",
        default=None,
        help="User email (default: current luci-auth user)",
    )
    p.add_argument(
        "-p",
        "--patch",
        default=None,
        help="Filter by Gerrit CL (any URL form, change ID, or crrev). "
        '"auto" detects from current branch; "none" clears the filter',
    )
    p.add_argument(
        "-s",
        "--status",
        default=None,
        help="Filter by status: Completed, Running, Failed, Cancelled, Queued",
    )
    p.add_argument(
        "-b",
        "--benchmark",
        default=None,
        help="Filter by benchmark name or alias (js3, js2, sp3)",
    )
    p.add_argument(
        "--bot",
        default=None,
        help="Filter by bot config or alias (m1, m2, m3, m4, linux)",
    )
    p.add_argument(
        "--since",
        default="one month ago",
        help='Only show jobs after this date (default: "one month ago"). '
        'Accepts natural language ("2 weeks ago") or ISO dates. Use "all" for no limit.',
    )
    p.set_defaults(func=_cmd_list_jobs)

    # show-results
    p = sub.add_parser(
        "show-results", aliases=["s"], help="Show base-vs-experiment comparison table"
    )
    p.add_argument(
        "job_urls",
        nargs="*",
        metavar="job_url",
        help="Pinpoint job URL(s) or job ID(s)",
    )
    p.add_argument(
        "--recent",
        type=int,
        default=None,
        metavar="N",
        help="Show results for the N most recent completed jobs",
    )
    p.add_argument(
        "-u",
        "--user",
        default=None,
        help="User email (default: current luci-auth user)",
    )
    p.add_argument(
        "-p",
        "--patch",
        default=None,
        help="Filter by Gerrit CL (any URL form, change ID, or crrev). "
        '"auto" detects from current branch; "none" clears the filter',
    )
    p.add_argument(
        "-s",
        "--status",
        default=None,
        help="Filter by status: Completed, Running, Failed, Cancelled, Queued",
    )
    p.add_argument(
        "-b",
        "--benchmark",
        default=None,
        help="Filter by benchmark name or alias (js3, js2, sp3)",
    )
    p.add_argument(
        "--bot",
        default=None,
        help="Filter by bot config or alias (m1, m2, m3, m4, linux)",
    )
    p.add_argument(
        "--since",
        default=None,
        help='Only include jobs after this date (default: "one month ago" when filters are used). '
        'Accepts natural language ("2 weeks ago") or ISO dates. Use "all" for no limit.',
    )
    p.add_argument(
        "--show-all", action="store_true", help="Include non-significant results"
    )
    p.add_argument(
        "--compact",
        action="store_true",
        help="Omit sig and direction columns (for pasting to docs)",
    )
    p.add_argument(
        "--use-cas",
        action="store_true",
        dest="use_cas",
        help="Fetch raw per-run data from CAS isolates for richer sub-metrics "
        "(Score/First/Average/Worst4 per story). Slower than the default "
        "scraping path and requires: gcloud auth application-default login",
    )
    p.set_defaults(func=_cmd_show_results)

    # create-job
    _template_names = ", ".join(pinpoint.BENCHMARK_ALIASES)
    p = sub.add_parser(
        "create-job", aliases=["c"], help="Create one or more Pinpoint A/B try jobs"
    )
    p.add_argument(
        "-t",
        "--template",
        nargs="+",
        metavar="TEMPLATE",
        default=["js3", "sp3"],
        help=f"Benchmark template(s) (default: js3 sp3): {_template_names}",
    )
    p.add_argument(
        "-b",
        "--benchmark",
        default=None,
        help="Benchmark name or alias (alternative to -t)",
    )
    p.add_argument(
        "-c",
        "--configuration",
        nargs="+",
        metavar="CONFIG",
        default=["m1"],
        help="Bot config(s) or alias(es) (default: m1)",
    )
    p.add_argument(
        "-s", "--story", default=None, help="Story within the benchmark (only with -b)"
    )
    p.add_argument(
        "--story-tags",
        default=None,
        dest="story_tags",
        help="Comma-separated story tags",
    )
    p.add_argument(
        "--base-git-hash",
        default=None,
        dest="base_git_hash",
        help="Base git hash (default: latest cached CI build)",
    )
    p.add_argument(
        "--exp-git-hash",
        default=None,
        dest="exp_git_hash",
        help="Experiment git hash (default: latest cached CI build)",
    )
    p.add_argument(
        "--base-patch",
        default=None,
        dest="base_patch",
        help="Gerrit patch for base (change ID, crrev/c/N, or URL)",
    )
    p.add_argument(
        "--exp-patch",
        nargs="+",
        default=None,
        dest="exp_patch",
        metavar="PATCH",
        help="Gerrit patch(es) for experiment. "
        '"auto" (default) detects from current branch; '
        '"none" for flag/hash-only comparisons',
    )
    p.add_argument(
        "--base-js-flags",
        default=None,
        dest="base_js_flags",
        help='V8 flags for base, e.g. "--turbofan"',
    )
    p.add_argument(
        "--exp-js-flags",
        nargs="+",
        default=None,
        dest="exp_js_flags",
        metavar="FLAGS",
        help="V8 flag set(s) for experiment",
    )
    p.add_argument(
        "-r",
        "--repeat",
        type=int,
        default=150,
        help="Bot runs per variant (default: 150)",
    )
    p.add_argument(
        "--bug-id", type=int, default=None, dest="bug_id", help="Buganizer issue ID"
    )
    p.add_argument(
        "-w",
        "--watch",
        action="store_true",
        default=None,
        help="Watch created job(s) and notify on completion "
        "(default: on when chat integration is configured)",
    )
    p.set_defaults(func=_cmd_create_job)

    # watch
    p = sub.add_parser("watch", help="Notify via webhook when a job completes")
    p.add_argument(
        "job_urls",
        nargs="+",
        metavar="job_url",
        help="Pinpoint job URL(s) or job ID(s)",
    )
    p.set_defaults(func=_cmd_watch)

    # chat-setup
    p = sub.add_parser(
        "chat-setup", help="Authenticate with Google Chat for direct notifications"
    )
    p.set_defaults(func=_cmd_chat_setup)

    # config
    p = sub.add_parser(
        "config", help=f"Print a config template (write to {config.CONFIG_PATH})"
    )
    p.set_defaults(func=_cmd_config)

    # upgrade
    p = sub.add_parser("upgrade", help="Upgrade pp to the latest version")
    p.set_defaults(func=_cmd_upgrade)

    # daemon-stop
    p = sub.add_parser("daemon-stop", help="Stop the background notification daemon")
    p.set_defaults(func=_cmd_daemon_stop)

    # logs
    p = sub.add_parser("logs", help="Show daemon log (use --follow to tail -f)")
    p.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    p.set_defaults(func=_cmd_logs)

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging (useful for debugging --use-cas)",
    )

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        for _noisy in ("httpx", "httpcore", "google.auth", "google.auth.transport"):
            logging.getLogger(_noisy).setLevel(logging.WARNING)
    from . import changelog

    changelog.show_unseen()

    try:
        args.func(args)
    except Exception as e:
        print(f"{_RED}error:{_RESET} {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
