"""pp — Pinpoint CLI wrapper around the v8-mcp tool functions.

Usage:
  pp show-job <job_url>
  pp list-jobs [--count N] [--user EMAIL] [--filter KEY=VALUE]
  pp get-raw-values <job_url>
  pp show-results <job_url> [--show-all]
  pp create-job --benchmark BENCH --configuration CONFIG [options]
  pp watch <job_url>
  pp daemon-stop
"""

from __future__ import annotations

import argparse
import json
import sys

import daemon

from tools import (
    pinpoint_create_job,
    pinpoint_get_raw_values,
    pinpoint_list_jobs,
    pinpoint_show_job,
    pinpoint_show_results,
)


def _out(result) -> None:
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, indent=2))


def _cmd_show_job(args: argparse.Namespace) -> None:
    _out(pinpoint_show_job(args.job_url))


def _cmd_list_jobs(args: argparse.Namespace) -> None:
    _out(pinpoint_list_jobs(count=args.count, user=args.user, filter=args.filter))


def _cmd_get_raw_values(args: argparse.Namespace) -> None:
    _out(pinpoint_get_raw_values(args.job_url))


def _cmd_show_results(args: argparse.Namespace) -> None:
    _out(pinpoint_show_results(args.job_url, show_all=args.show_all))


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
        print(f"Watching {result.get('jobId') or job_url} — you'll be notified on completion.")


def _cmd_watch(args: argparse.Namespace) -> None:
    if not daemon.is_running():
        daemon.start_background()
    daemon.send_job(args.job_url)
    job_id = args.job_url.split("/")[-1]
    print(f"Watching {job_id} — you'll be notified on completion.")


def _cmd_daemon_stop(args: argparse.Namespace) -> None:
    import os, signal
    if not daemon.is_running():
        print("Daemon is not running.")
        return
    pid = int(daemon.PID_PATH.read_text())
    os.kill(pid, signal.SIGTERM)
    print(f"Stopped daemon (pid {pid}).")


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

    # get-raw-values
    p = sub.add_parser("get-raw-values", help="Dump per-run raw measurement values as JSON")
    p.add_argument("job_url", help="Pinpoint job URL or job ID")
    p.set_defaults(func=_cmd_get_raw_values)

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
    p.add_argument("-r", "--repeat", type=int, default=30,
                   help="Bot runs per variant (default: 30)")
    p.add_argument("--bug-id", type=int, default=None, dest="bug_id",
                   help="Buganizer issue ID")
    p.add_argument("-w", "--watch", action="store_true",
                   help="Watch the created job and notify on completion")
    p.set_defaults(func=_cmd_create_job)

    # watch
    p = sub.add_parser("watch", help="Notify via webhook when a job completes")
    p.add_argument("job_url", help="Pinpoint job URL or job ID")
    p.set_defaults(func=_cmd_watch)

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
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
