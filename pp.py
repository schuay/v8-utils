"""pp — Pinpoint CLI wrapper around the v8-utils tool functions.

Usage:
  pp show-job <job_url>
  pp list-jobs [--count N] [--user EMAIL] [--filter KEY=VALUE]
  pp show-results <job_url> [--show-all]
  pp create-job --benchmark BENCH --configuration CONFIG [options]
  pp watch <job_url>
  pp daemon-stop
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

import daemon
import pinpoint

from tools import (
    pinpoint_create_job,
    pinpoint_list_jobs,
    pinpoint_show_job,
    pinpoint_show_results,
)

# ── ANSI colors (no-ops when not a TTY) ───────────────────────────────────────

if sys.stdout.isatty():
    _BOLD   = "\033[1m"
    _DIM    = "\033[2m"
    _RED    = "\033[31m"
    _GREEN  = "\033[32m"
    _YELLOW = "\033[33m"
    _CYAN   = "\033[36m"
    _RESET  = "\033[0m"
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
    r'("(?:[^"\\]|\\.)*")\s*:'          # key
    r'|("(?:[^"\\]|\\.)*")'             # string value
    r'|(true|false|null)'               # boolean / null
    r'|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'  # number
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


def _colorize_results(text: str) -> str:
    if not _CYAN:
        return text

    def _color_pct(m: re.Match) -> str:
        val = m.group(0)
        return f"{_GREEN}{val}{_RESET}" if val.startswith("+") else f"{_RED}{val}{_RESET}"

    out = []
    for line in text.splitlines():
        if line.startswith("base:") or line.startswith("exp:"):
            key, _, rest = line.partition(":")
            out.append(f"{_DIM}{key}:{_RESET} {_CYAN}{rest.strip()}{_RESET}")
        elif re.fullmatch(r"-+", line):
            out.append(f"{_DIM}{line}{_RESET}")
        elif "chg%" in line:
            out.append(f"{_BOLD}{line}{_RESET}")
        else:
            line = re.sub(r"[+-]\d+\.\d+%", _color_pct, line)
            line = re.sub(r"\*\s*$", f"{_BOLD}{_GREEN}*{_RESET}", line)
            out.append(line)
    return "\n".join(out)


# ── Output helpers ─────────────────────────────────────────────────────────────

def _out(result) -> None:
    if isinstance(result, str):
        print(result)
    else:
        print(_colorize_json(json.dumps(result, indent=2)))


# ── Command handlers ───────────────────────────────────────────────────────────

def _cmd_show_job(args: argparse.Namespace) -> None:
    j = pinpoint_show_job(args.job_url)
    url = f"{_CYAN}https://pinpoint-dot-chromeperf.appspot.com/job/{j.get('job_id')}{_RESET}"
    created = (j.get("created") or "")[:16].replace("T", " ")
    status = j.get("status") or "?"
    print(f"{_DIM}{created}{_RESET}  {_status_color(status)}  {url}")
    print()

    patch_url = j.get("experiment_patch")
    patch_subject = pinpoint.fetch_gerrit_subject(patch_url) if patch_url else None

    patch_val = patch_url
    if patch_val and patch_subject:
        patch_val = f"{patch_url}  {_BOLD}\"{patch_subject}\"{_RESET}"

    fields = [
        ("configuration", j.get("configuration")),
        ("benchmark",     j.get("benchmark")),
        ("story",         j.get("story")),
        ("mode",          j.get("comparison_mode")),
        ("base",          j.get("base_git_hash")),
        ("end",           j.get("end_git_hash")),
        ("patch",         patch_val),
        ("base-flags",    j.get("base_extra_args")),
        ("exp-flags",     j.get("experiment_extra_args")),
        ("diffs",         j.get("difference_count")),
        ("bug",           j.get("bug_id")),
        ("results",       j.get("results_url")),
        ("exception",     j.get("exception")),
    ]
    w = max((len(k) for k, v in fields if v is not None), default=0)
    for key, val in fields:
        if val is None:
            continue
        val_str = f"{_CYAN}{val}{_RESET}" if key in ("patch", "results") else str(val)
        print(f"  {_DIM}{key:<{w}}{_RESET}  {val_str}")


def _cmd_list_jobs(args: argparse.Namespace) -> None:
    import concurrent.futures
    jobs = pinpoint_list_jobs(count=args.count, user=args.user, filter=args.filter)
    if not jobs:
        print("No jobs found.")
        return

    patches = [j.get("experiment_patch") or "" for j in jobs]
    with concurrent.futures.ThreadPoolExecutor() as ex:
        subjects = list(ex.map(
            lambda p: pinpoint.fetch_gerrit_subject(p) if p else None,
            patches,
        ))

    for j, subject in zip(jobs, subjects):
        created = (j.get("created") or "")[:16].replace("T", " ")
        status = j.get("status") or "?"
        url = j.get("url") or ""
        config_ = j.get("configuration") or ""
        benchmark = j.get("benchmark") or ""
        story = j.get("story") or ""
        diff = j.get("difference_count")
        patch = j.get("experiment_patch") or ""
        base_flags = j.get("base_extra_args") or ""
        exp_flags = j.get("experiment_extra_args") or ""

        label = f"{benchmark} / {story}".strip(" /")
        diff_str = f"  {_YELLOW}diffs={diff}{_RESET}" if diff is not None else ""
        print(f"{_DIM}{created}{_RESET}  {_status_color(f'{status:<12}')}  {_CYAN}{url}{_RESET}")
        print(f"  {_DIM}{config_}{_RESET}  {_BOLD}{label}{_RESET}{diff_str}")
        if patch:
            subject_str = f"  {_BOLD}\"{subject}\"{_RESET}" if subject else ""
            print(f"  {_DIM}patch:{_RESET}      {_CYAN}{patch}{_RESET}{subject_str}")
        if base_flags:
            print(f"  {_DIM}base-flags:{_RESET} {base_flags}")
        if exp_flags:
            print(f"  {_DIM}exp-flags:{_RESET}  {exp_flags}")
        print()



def _cmd_show_results(args: argparse.Namespace) -> None:
    result = pinpoint_show_results(args.job_url, show_all=args.show_all)
    if isinstance(result, str):
        print(_colorize_results(result))
    else:
        _out(result)


def _cmd_create_job(args: argparse.Namespace) -> None:
    result = pinpoint_create_job(
        benchmark=args.benchmark,
        configuration=args.configuration,
        story=args.story,
        story_tags=args.story_tags,
        base_git_hash=args.base_git_hash,
        exp_git_hash=args.exp_git_hash,
        base_patch=args.base_patch,
        exp_patch=args.exp_patch,
        base_js_flags=args.base_js_flags,
        exp_js_flags=args.exp_js_flags,
        repeat=args.repeat,
        bug_id=args.bug_id,
    )
    _out(result)
    if args.watch and (job_url := result.get("url")):
        if not daemon.is_running():
            daemon.start_background()
        daemon.send_job(job_url)
        print(f"{_GREEN}Watching{_RESET} {result.get('jobId') or job_url} — you'll be notified on completion.")


def _cmd_watch(args: argparse.Namespace) -> None:
    if not daemon.is_running():
        daemon.start_background()
    daemon.send_job(args.job_url)
    job_id = args.job_url.split("/")[-1]
    print(f"{_GREEN}Watching{_RESET} {job_id} — you'll be notified on completion.")


def _cmd_daemon_stop(args: argparse.Namespace) -> None:
    import signal
    if not daemon.is_running():
        print(f"{_YELLOW}Daemon is not running.{_RESET}")
        return
    pid = int(daemon.PID_PATH.read_text())
    os.kill(pid, signal.SIGTERM)
    print(f"{_GREEN}Stopped daemon{_RESET} (pid {pid}).")


def _cmd_upgrade(args: argparse.Namespace) -> None:
    os.execvp("uv", ["uv", "tool", "install",
                     "git+https://github.com/schuay/v8-utils.git", "--reinstall"])


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
    parser = argparse.ArgumentParser(prog="pp", description="Pinpoint CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # show-job
    p = sub.add_parser("show-job", help="Show details of a Pinpoint job")
    p.add_argument("job_url", help="Pinpoint job URL or job ID")
    p.set_defaults(func=_cmd_show_job)

    # list-jobs
    p = sub.add_parser("list-jobs", help="List recent Pinpoint jobs for a user")
    p.add_argument("-n", "--count", type=int, default=20, metavar="N",
                   help="Number of jobs (default: 20)")
    p.add_argument("-u", "--user", default=None,
                   help="User email (default: current luci-auth user)")
    p.add_argument("-f", "--filter", default=None, metavar="KEY=VALUE",
                   help='Client-side filter, e.g. "status=Completed", "comparison_mode=try"')
    p.set_defaults(func=_cmd_list_jobs)

    # show-results
    p = sub.add_parser("show-results", help="Show base-vs-experiment comparison table")
    p.add_argument("job_url", help="Pinpoint job URL or job ID")
    p.add_argument("--show-all", action="store_true",
                   help="Include non-significant results")
    p.set_defaults(func=_cmd_show_results)

    # create-job
    p = sub.add_parser("create-job", help="Create a new Pinpoint A/B try job")
    p.add_argument("-b", "--benchmark", required=True,
                   help='Benchmark name or alias ("js3" = jetstream-main.crossbench)')
    p.add_argument("-c", "--configuration", required=True,
                   help='Bot config or alias ("linux", "macm4")')
    p.add_argument("-s", "--story", default=None,
                   help="Story within the benchmark")
    p.add_argument("--story-tags", default=None, dest="story_tags",
                   help="Comma-separated story tags")
    p.add_argument("--base-git-hash", default="HEAD", dest="base_git_hash",
                   help="Base git hash (default: HEAD)")
    p.add_argument("--exp-git-hash", default="HEAD", dest="exp_git_hash",
                   help="Experiment git hash (default: HEAD)")
    p.add_argument("--base-patch", default=None, dest="base_patch",
                   help="Gerrit patch for base (change ID, crrev/c/N, or URL)")
    p.add_argument("--exp-patch", default=None, dest="exp_patch",
                   help="Gerrit patch for experiment")
    p.add_argument("--base-js-flags", default=None, dest="base_js_flags",
                   help='V8 flags for base, e.g. "--turbofan"')
    p.add_argument("--exp-js-flags", default=None, dest="exp_js_flags",
                   help="V8 flags for experiment")
    p.add_argument("-r", "--repeat", type=int, default=100,
                   help="Bot runs per variant (default: 100)")
    p.add_argument("--bug-id", type=int, default=None, dest="bug_id",
                   help="Buganizer issue ID")
    p.add_argument("-w", "--watch", action="store_true",
                   help="Watch the created job and notify on completion")
    p.set_defaults(func=_cmd_create_job)

    # watch
    p = sub.add_parser("watch", help="Notify via webhook when a job completes")
    p.add_argument("job_url", help="Pinpoint job URL or job ID")
    p.set_defaults(func=_cmd_watch)

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

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f"{_RED}error:{_RESET} {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
