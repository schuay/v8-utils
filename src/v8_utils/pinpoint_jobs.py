"""Pinpoint job operations shared by the command line and the MCP server:
fetching job details and listings, resolving patch arguments, and creating
and cancelling jobs."""

import logging
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from . import config
from . import pinpoint
from .concurrency import _run_concurrent


def fetch_job_details_sorted(
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
            return fetch_job_detail(jid)
        except Exception as e:
            log.debug("fetch_job_detail failed for %s", jid, exc_info=True)
            return {"job_id": jid, "error": str(e)}

    fns = [lambda jid=jid: fetch(jid) for jid in job_ids]
    details = _run_concurrent(fns, on_progress)
    paired = list(zip(job_ids, details))
    paired.sort(key=lambda p: p[1].get("created") or "")
    return paired


def fetch_job_detail(job_url: str) -> dict:
    """One job's details as a flat dict; fields Pinpoint left unset are absent."""
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
        "base_patch": args.get("base_patch"),
        "experiment_patch": args.get("experiment_patch"),
        "base_extra_args": args.get("base_extra_args"),
        "experiment_extra_args": args.get("experiment_extra_args"),
        "difference_count": data.get("difference_count"),
        "exception": data.get("exception"),
        "bug_id": data.get("bug_id"),
        "results_url": data.get("results_url"),
    }
    return {k: v for k, v in result.items() if v is not None}


def fetch_jobs_list(
    count: int = 20,
    user: str | None = None,
    filters: list[str] | None = None,
    since: datetime | None = None,
) -> list[dict]:
    """A user's recent jobs, newest first, summarised as dicts."""
    if user is None:
        user = config.load().user or pinpoint.get_current_user_email()
    return [
        pinpoint.summarise_job(j)
        for j in pinpoint.fetch_jobs(user, count, filters, since=since)
    ]


def get_gerrit_issue_url(
    cwd: str | None = None, branch: str | None = None
) -> str | None:
    """Read the Gerrit CL URL for a git branch from git config.

    Defaults to the current branch (HEAD) when branch is None.
    Returns a full URL including patchset, e.g.:
      https://chromium-review.googlesource.com/7650974/1
    Returns None if not inside a git repo or the branch has no associated CL.
    """

    def _git(*args: str) -> str:
        r = subprocess.run(
            ["git"] + list(args), capture_output=True, text=True, cwd=cwd
        )
        return r.stdout.strip() if r.returncode == 0 else ""

    if branch is None:
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


def get_gerrit_parent_url(cwd: str | None = None) -> str | None:
    """Read the Gerrit CL URL of the current branch's upstream (parent) branch.

    For a stacked branch, the upstream branch is the parent CL it builds on.
    Returns None if there is no upstream branch or it has no associated CL.
    """
    r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    parent = r.stdout.strip() if r.returncode == 0 else ""
    if not parent:
        return None
    return get_gerrit_issue_url(cwd=cwd, branch=parent)


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


def resolve_base_patch(value: str | None, cwd: str | None = None) -> str | None:
    """Resolve a base_patch value, additionally supporting the "parent" sentinel.

    "parent" detects the CL of the current branch's upstream (parent) branch,
    which is the base for a stacked CL.  "none"/None -> None, "auto" -> current
    branch CL, anything else is passed through unchanged.

    Raises ValueError if "parent" is used but no parent CL can be found.
    """
    if value is None:
        return None
    if value.lower() == "parent":
        detected = get_gerrit_parent_url(cwd=cwd)
        if detected is None:
            raise ValueError(
                "No parent Gerrit CL found for --base-patch=parent.\n"
                "The current branch's upstream branch has no associated CL.\n"
                "Either:\n"
                "  - pass --base-patch with an explicit CL URL\n"
                "  - check the upstream branch with: git branch -vv"
            )
        return detected
    return _resolve_patch_sentinel(value, cwd=cwd)


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

    # Validate every configuration up front.  Creating jobs one at a time means
    # a bad name in the middle of the list would otherwise leave the earlier
    # ones running, which reads as a complete batch.
    unknown = [
        c
        for c in configurations
        if pinpoint.CONFIGURATION_ALIASES.get(c, c)
        not in pinpoint.known_configurations()
    ]
    if unknown:
        known = ", ".join(sorted(pinpoint.CONFIGURATION_ALIASES))
        raise ValueError(
            f"Unknown bot configuration(s): {', '.join(unknown)}. "
            f"Known aliases: {known}."
        )

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

    # Pre-fetch Gerrit subjects for human-readable job names.  Unlike the
    # display paths, a miss here warns rather than logging at debug: the subject
    # is baked into the job title on Pinpoint and cannot be edited afterwards,
    # so a silent miss leaves a job permanently named after an unidentifiable CL.
    patch_subjects: dict[str, str | None] = {}

    def _subject_for_name(p: str) -> str | None:
        try:
            return pinpoint.fetch_gerrit_subject(p)
        except Exception as e:
            print(
                f"warning: could not fetch Gerrit subject for {p}: {e}",
                file=sys.stderr,
            )
            change = pinpoint._extract_change_id(p)
            return f"CL {change}" if change else None

    for p in exp_patches:
        if p and p not in patch_subjects:
            patch_subjects[p] = _subject_for_name(p)
    if base_patch and base_patch not in patch_subjects:
        patch_subjects[base_patch] = _subject_for_name(base_patch)

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
        parts.append(f"({cfg}, {pinpoint.short_benchmark(bench)})")
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
            job_detail = fetch_job_detail(job_url)
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
                if on_watching:
                    on_watching(url)

    return jobs


@dataclass(frozen=True)
class Cancelled:
    job_id: str
    #: Pinpoint's state after the request, None when it failed.
    state: str | None
    error: str | None = None


def cancel_jobs(
    job_urls: list[str],
    reason: str = "Cancelled",
    on_progress: Callable[[int, int], None] | None = None,
) -> list[Cancelled]:
    """Cancel each job, concurrently. A failure is reported per job rather
    than raised, so one bad id does not hide the outcome of the others."""

    def cancel(url: str) -> Cancelled:
        try:
            result = pinpoint.cancel_job(url, reason=reason)
            job_id = result.get("job_id", pinpoint.job_id_from_url(url))
            return Cancelled(job_id=job_id, state=result.get("state", "unknown"))
        except Exception as e:
            return Cancelled(
                job_id=pinpoint.job_id_from_url(url), state=None, error=str(e)
            )

    return _run_concurrent([lambda u=u: cancel(u) for u in job_urls], on_progress)
