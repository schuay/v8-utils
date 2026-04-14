"""Pinpoint performance infrastructure — data access and processing helpers."""

from __future__ import annotations

import concurrent.futures
import json
import re
import statistics
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from scipy.stats import false_discovery_control, mannwhitneyu

_PINPOINT_BASE = "https://pinpoint-dot-chromeperf.appspot.com"
_GERRIT_BASE = "https://chromium-review.googlesource.com"

_LOGIN_INSTRUCTIONS = (
    "Not logged in via luci-auth. "
    "Run:  luci-auth login -scopes https://www.googleapis.com/auth/userinfo.email"
)

_TERMINAL_STATES = {"Completed", "Failed", "Cancelled"}


# ── LUCI auth ─────────────────────────────────────────────────────────────────


def _luci_run(command: str) -> str:
    """Run a luci-auth subcommand and return stdout, or raise ValueError."""
    try:
        return subprocess.check_output(
            ["luci-auth", command], stderr=subprocess.STDOUT, text=True
        )
    except subprocess.CalledProcessError as e:
        raise ValueError(e.output.strip() or _LOGIN_INSTRUCTIONS)
    except FileNotFoundError:
        raise ValueError("luci-auth not found in PATH. " + _LOGIN_INSTRUCTIONS)


def get_current_user_email() -> str:
    """Return the email of the currently logged-in user, preferring chromium.org."""
    token = _luci_run("token").strip()
    r = httpx.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()
    email = r.json().get("email")
    if not email:
        raise ValueError("Could not retrieve email from userinfo API")
    if email.endswith("@google.com"):
        chromium_email = email.split("@")[0] + "@chromium.org"
        if get_auth_headers(chromium_email):
            return chromium_email
    return email


def get_auth_headers(email: str | None = None) -> dict[str, str]:
    """Return Authorization headers for the given email (or current LUCI user).

    Pass email to request a token for a specific account via luci-auth -email.
    Returns {} if not logged in or the account is unavailable.
    """
    try:
        cmd = ["luci-auth", "token"]
        if email:
            cmd += ["-email", email]
        token = subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, text=True
        ).strip()
        return {"Authorization": f"Bearer {token}"}
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}


def user_email_variants(email: str) -> list[str]:
    """Return email plus its @google.com and @chromium.org counterparts."""
    username = email.split("@")[0]
    variants = [email, f"{username}@google.com", f"{username}@chromium.org"]
    return list(dict.fromkeys(variants))  # deduplicate, preserve order


# ── Gerrit patch resolver ──────────────────────────────────────────────────────


def _parse_change_patchset(path: str) -> tuple[str, str | None] | None:
    """Extract (change_id, patchset) from a path segment like /CHANGE[/PATCHSET].

    Returns None if the first path component is not a numeric change ID.
    """
    parts = [p for p in path.strip("/").split("/") if p]
    if parts and parts[0].isdigit():
        patchset = parts[1] if len(parts) > 1 and parts[1].isdigit() else None
        return parts[0], patchset
    return None


def resolve_patch(patch: str) -> str:
    """Resolve a Gerrit patch shorthand to a full chromium-review URL.

    Accepts:
      12345                                                    bare change ID
      12345/1                                                  change ID + patchset
      c/12345[/1]                                              Gerrit short path
      https://crrev.com/c/12345[/1]                           crrev URL
      https://chromium-review.googlesource.com/12345[/1]      short Gerrit URL
      https://chromium-review.googlesource.com/c/v8/v8/+/...  canonical (pass-through)
    """
    patch = patch.strip()

    def _resolve_change_id(change_id: str, patchset: str | None) -> str:
        r = httpx.get(f"{_GERRIT_BASE}/changes/v8%2Fv8~{change_id}", timeout=15)
        r.raise_for_status()
        text = r.text[r.text.find("{") :]  # strip Gerrit's XSSI prefix ")]}'"
        project = json.loads(text)["project"]
        url = f"{_GERRIT_BASE}/c/{project}/+/{change_id}"
        return f"{url}/{patchset}" if patchset else url

    parsed = urlparse(patch)

    if parsed.scheme in ("http", "https"):
        host = parsed.hostname or ""

        if host == "crrev.com":
            # https://crrev.com/c/CHANGE[/PATCHSET]
            path = parsed.path.lstrip("/")
            if path.startswith("c/"):
                path = path[2:]
            result = _parse_change_patchset(path)
            if result:
                return _resolve_change_id(*result)

        if host in (
            "chromium-review.googlesource.com",
            "chromium-review.git.corp.google.com",
        ):
            if parsed.path.startswith("/c/"):
                # Already canonical — strip any query/fragment and return
                return f"{_GERRIT_BASE}{parsed.path}"
            # Short form: /CHANGE[/PATCHSET]
            result = _parse_change_patchset(parsed.path)
            if result:
                return _resolve_change_id(*result)

        # Unknown or already-canonical http URL — pass through
        return patch

    # No scheme: bare change ID, c/CHANGE[/PATCHSET], or CHANGE/PATCHSET
    path = patch.lstrip("/")
    if path.startswith("c/"):
        path = path[2:]
    result = _parse_change_patchset(path)
    if result:
        return _resolve_change_id(*result)

    raise ValueError(
        f"Unrecognised patch format: {patch!r}. "
        "Expected a change ID (12345), crrev/c/12345, or a full Gerrit URL."
    )


def _extract_patch_fields(
    patch: str,
) -> tuple[str | None, str | None, str | None]:
    """Extract (project, change_id, patchset) from any supported patch form.

    Handles: bare change ID, CHANGE/PATCHSET, crrev.com URLs, full Gerrit URLs.
    Returns (None, None, None) if no numeric change ID can be found.
    """
    from . import pinpoint_cache

    return pinpoint_cache.parse_patch_fields(patch)


def _gerrit_change_id_from_url(url: str) -> str | None:
    """Extract a Gerrit change ID from any supported URL format, or return None.

    Returns "project~change_id" when the project is present in the URL
    (e.g. /c/v8/v8/+/123), or bare "change_id" otherwise.
    """
    project, change, _ = _extract_patch_fields(url)
    if change is None:
        return None
    if project:
        return f"{project.replace('/', '%2F')}~{change}"
    return change


def _extract_change_and_patchset(
    patch: str,
) -> tuple[str, str | None] | None:
    """Extract (change_id, patchset) from any supported patch form.

    Legacy wrapper — returns (change_id, patchset) without project.
    Returns None if no numeric change ID can be found.
    """
    _, change, patchset = _extract_patch_fields(patch)
    return (change, patchset) if change else None


def _extract_change_id(patch: str) -> str | None:
    """Extract a numeric Gerrit change ID from any supported patch form."""
    _, change, _ = _extract_patch_fields(patch)
    return change


def fetch_gerrit_subject(patch_url: str) -> str | None:
    """Return the subject (first line of commit message) for a Gerrit change URL.

    Returns None if the change ID cannot be extracted or the request fails.
    """
    change_id = _gerrit_change_id_from_url(patch_url)
    if not change_id:
        return None
    try:
        r = httpx.get(f"{_GERRIT_BASE}/changes/{change_id}", timeout=15)
        r.raise_for_status()
        text = r.text[r.text.find("{") :]
        return json.loads(text).get("subject")
    except Exception:
        return None


# ── Job listing ───────────────────────────────────────────────────────────────


def job_id_from_url(job_url: str) -> str:
    """Extract the job ID from a Pinpoint job URL, or return the input unchanged."""
    m = re.search(r"/job/([a-zA-Z0-9]+)", job_url)
    return m.group(1) if m else job_url


def fetch_job(job_id: str) -> dict[str, Any]:
    """Fetch raw job JSON, using the cache for terminal jobs."""
    from . import pinpoint_cache

    cached = pinpoint_cache.get_job(job_id)
    if cached and cached.get("status") in _TERMINAL_STATES:
        # /results2/ is a transient results_url while Pinpoint is still
        # generating the full results HTML; re-fetch to pick up the final URL.
        results_url = cached.get("results_url") or ""
        if not results_url.startswith("/results2/"):
            return cached
    r = httpx.get(
        f"{_PINPOINT_BASE}/api/job/{job_id}", follow_redirects=True, timeout=30
    )
    r.raise_for_status()
    data = r.json()
    pinpoint_cache.put_job(data)
    return data


def _is_cq_job(job: dict) -> bool:
    tags_raw = job.get("arguments", {}).get("tags", "")
    try:
        tags = json.loads(tags_raw) if tags_raw else {}
    except (ValueError, TypeError):
        tags = {}
    return tags.get("origin") == "CQ"


def _job_matches_filter(job: dict, filter_str: str) -> bool:
    """Test a job against a "key=value" filter string (case-insensitive substring).

    Supported keys: status, benchmark, bot/configuration, comparison_mode, patch.

    - "patch" normalises to a numeric change ID so any Gerrit URL form matches.
    - "benchmark" resolves aliases (js3, js2, sp3) before matching.
    - "bot" is an alias for "configuration" and resolves bot aliases (m1, linux, …).
    """
    if "=" not in filter_str:
        return True
    key, _, value = filter_str.partition("=")
    key, value = key.strip().lower(), value.strip()
    args = job.get("arguments", {})

    if key == "patch":
        needle = _extract_change_and_patchset(value)
        stored = args.get("experiment_patch") or args.get("base_patch") or ""
        stored_parsed = _extract_change_and_patchset(stored)
        if needle and stored_parsed:
            if needle[0] != stored_parsed[0]:
                return False
            # If the filter specifies a patchset, require it to match
            if needle[1] is not None and needle[1] != stored_parsed[1]:
                return False
            return True
        # Fallback: substring match
        needle_str = needle[0] if needle else value.lower()
        return needle_str in stored.lower()

    if key == "benchmark":
        alias = BENCHMARK_ALIASES.get(value)
        if alias:
            value = alias[0]  # full benchmark name

    if key in ("bot", "configuration"):
        key = "configuration"
        resolved = CONFIGURATION_ALIASES.get(value)
        if resolved:
            value = resolved

    field = {
        "status": job.get("status", ""),
        "benchmark": args.get("benchmark", ""),
        "configuration": job.get("configuration", ""),
        "comparison_mode": job.get("comparison_mode", ""),
    }.get(key, "")
    return value.lower() in field.lower()


def _parse_created(created: str) -> datetime:
    """Parse a Pinpoint job's 'created' timestamp to a UTC datetime."""
    # Strip trailing 'Z' or timezone info and parse as UTC.
    s = created.rstrip("Z").split("+")[0]
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


_DEFAULT_SINCE = timedelta(days=30)


def parse_since(value: str) -> datetime:
    """Parse a --since value into a UTC datetime.

    Uses dateparser for natural language support. Accepts anything dateparser
    understands, e.g. "one month ago", "2 weeks ago", "2026-03-01", "yesterday".

    The special value "all" disables the cutoff (returns datetime.min).
    """
    import dateparser

    value = value.strip()
    if value.lower() == "all":
        return datetime.min
    dt = dateparser.parse(value, settings={"RETURN_AS_TIMEZONE_AWARE": True})
    if dt is None:
        raise ValueError(
            f"Could not parse --since value: {value!r}. "
            'Try "2 weeks ago", "2026-03-01", or "all".'
        )
    return dt


def _fetch_jobs_for_email(email: str, since: datetime | None) -> None:
    """Fetch jobs for an email, ensuring the cache covers [since, now].

    Maintains the invariant that jobs in the cache range [floor, ceiling]
    are exhaustive — every job in that time range is present.
    """
    from . import pinpoint_cache

    ceiling, floor = pinpoint_cache.get_range(email)
    since_str = since.isoformat() if since else None
    params: dict = {"filter": f"user={email}"}
    newest_seen = ceiling  # track the newest timestamp we've seen

    def _paginate_until(stop_at: str | None) -> bool:
        """Paginate from current cursor until we see a job with created <= stop_at.

        Stores all fetched jobs. Returns True if we ran out of pages
        (no more jobs exist on the server).
        """
        nonlocal params, newest_seen
        while True:
            r = httpx.get(
                f"{_PINPOINT_BASE}/api/jobs",
                params=params,
                follow_redirects=True,
                timeout=30,
            )
            if r.status_code >= 500:
                import sys

                print(
                    f"warning: Pinpoint API returned {r.status_code} during "
                    f"pagination, returning partial results",
                    file=sys.stderr,
                )
                return True
            r.raise_for_status()
            try:
                data = r.json()
            except Exception:
                return True  # non-JSON response, treat as end of pages
            raw_page = data.get("jobs", [])
            if not raw_page:
                return True

            pinpoint_cache.put_jobs(raw_page)

            # Track newest seen for ceiling update
            first = raw_page[0].get("created", "")
            if first and (not newest_seen or first > newest_seen):
                newest_seen = first

            # Check if we've reached the stop point
            last = raw_page[-1].get("created", "")
            if stop_at and last and last <= stop_at:
                return False

            next_cursor = data.get("next_cursor")
            if (
                not data.get("next")
                or not next_cursor
                or next_cursor == params.get("next_cursor")
            ):
                return True
            params["next_cursor"] = next_cursor

    if ceiling:
        # Phase 1: close the gap from now down to ceiling
        exhausted = _paginate_until(ceiling)
        # Phase 2: if since is older than floor, extend downward
        if not exhausted and since_str and (not floor or since_str < floor):
            _paginate_until(since_str)
    else:
        # Cold cache: fetch from now down to since
        _paginate_until(since_str)

    # Update the range
    new_ceiling = newest_seen or ""
    if since_str and (not floor or since_str < floor):
        new_floor = since_str
    else:
        new_floor = floor or since_str or ""
    if new_ceiling:
        pinpoint_cache.set_range(email, new_ceiling, new_floor)


def fetch_jobs(
    user: str,
    count: int,
    filters: list[str] | None = None,
    since: datetime | None = None,
) -> list[dict]:
    """Fetch the `count` most recent non-CQ jobs for a user.

    Uses a local SQLite cache to avoid re-fetching terminal jobs.
    Queries all email variants (@google.com, @chromium.org) and merges.
    The /api/jobs endpoint is public; no auth required.

    filters: list of "key=value" strings, ANDed together.
    since:   only return jobs created on or after this datetime (default: 30 days ago).
             Pass since=datetime.min to disable the cutoff.
    """
    from . import pinpoint_cache

    if since is None:
        since = datetime.now(timezone.utc) - _DEFAULT_SINCE
    if since == datetime.min:
        since = None
    variants = user_email_variants(user)

    # 1. Ensure cache covers [since, now] for all email variants
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(variants)) as ex:
        list(
            ex.map(
                lambda e: _fetch_jobs_for_email(e, since),
                variants,
            )
        )

    # 2. Re-fetch non-terminal cached jobs to update status
    stale = pinpoint_cache.query_jobs(
        users=variants,
        exclude_statuses=list(_TERMINAL_STATES),
    )
    if stale:

        def _refresh(job: dict) -> None:
            try:
                fetch_job(job["job_id"])  # fetches from API + caches
            except Exception:
                pass

        with concurrent.futures.ThreadPoolExecutor() as ex:
            list(ex.map(_refresh, stale))

    # 3. Extract patch filter for SQL query (if present)
    change, patchset = None, None
    remaining_filters = []
    if filters:
        for f in filters:
            if f.startswith("patch="):
                result = _extract_change_and_patchset(f.partition("=")[2])
                if result:
                    change, patchset = result
            else:
                remaining_filters.append(f)

    # 4. Query cache
    since_str = since.isoformat() if since else None
    all_jobs = pinpoint_cache.query_jobs(
        users=variants,
        since=since_str,
        change=change,
        patchset=patchset,
    )
    # Client-side filters for fields not in the DB schema
    all_jobs = [j for j in all_jobs if not _is_cq_job(j)]
    if remaining_filters:
        all_jobs = [
            j
            for j in all_jobs
            if all(_job_matches_filter(j, f) for f in remaining_filters)
        ]

    # 5. Prune old entries periodically
    pinpoint_cache.prune()
    return all_jobs[:count]


def summarise_job(j: dict) -> dict:
    """Extract the key fields from a raw job dict."""
    args = j.get("arguments", {})
    return {
        "job_id": j.get("job_id"),
        "url": f"{_PINPOINT_BASE}/job/{j.get('job_id')}",
        "name": j.get("name"),
        "status": j.get("status"),
        "created": j.get("created"),
        "configuration": j.get("configuration"),
        "benchmark": args.get("benchmark"),
        "story": args.get("story"),
        "base_git_hash": args.get("base_git_hash"),
        "experiment_patch": args.get("experiment_patch"),
        "base_extra_args": args.get("base_extra_args"),
        "experiment_extra_args": args.get("experiment_extra_args"),
        "difference_count": j.get("difference_count"),
        "exception": j.get("exception"),
    }


# ── Histogram parsing ─────────────────────────────────────────────────────────


def fetch_histograms(job_id: str) -> tuple[list[dict], dict[str, Any]]:
    """Fetch and parse histogram entries for a completed Pinpoint job.

    Returns (histograms, guids) where guids maps GUID → resolved label value.
    Raises ValueError if the job is not yet completed or has no results.
    """
    job = fetch_job(job_id)
    status = job.get("status", "Unknown")
    if status != "Completed":
        raise ValueError(f"Job is not completed (status: {status})")

    results_path = job.get("results_url")
    if not results_path:
        raise ValueError("Job has no results_url")

    r = httpx.get(_PINPOINT_BASE + results_path, follow_redirects=True, timeout=60)
    r.raise_for_status()

    # Histogram data is NDJSON embedded in the last HTML comment block.
    # If this ever starts failing, switch to CAS: each bot run stores a CAS
    # isolate with the raw crossbench output (requires gcloud ADC + cas CLI).
    comments = re.findall(r"<!--(.*?)-->", r.text, re.DOTALL)
    data_block = next(
        (c for c in reversed(comments) if c.lstrip().startswith("{")), None
    )
    if not data_block:
        raise ValueError(
            "Results page has no histogram data yet. "
            "This usually means Pinpoint is still generating results — "
            "try again in a few minutes, or use use_cas=True to fetch raw data from CAS."
        )

    entries = [json.loads(line) for line in data_block.splitlines() if line.strip()]
    guids = {
        e["guid"]: e["values"][0] if len(e["values"]) == 1 else e["values"]
        for e in entries
        if e.get("type") == "GenericSet"
    }
    histograms = [e for e in entries if "name" in e and "unit" in e]
    return histograms, guids


def _collect_groups(histograms: list[dict], guids: dict) -> dict[tuple[str, str], dict]:
    """Group histogram sample values by (metric_name, label)."""
    groups: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"unit": None, "values": []}
    )
    for h in histograms:
        diag = h.get("diagnostics", {})
        label = guids.get(diag.get("labels"), diag.get("labels", "unknown"))
        key = (h["name"], label)
        groups[key]["unit"] = h["unit"]
        groups[key]["values"].extend(h.get("sampleValues", []))
    return groups


def _value_stats(vals: list[float]) -> dict:
    return {
        "mean": statistics.mean(vals) if vals else None,
        "stdev": statistics.stdev(vals) if len(vals) > 1 else None,
        "n": len(vals),
    }


def _apply_significance(
    rows: list[dict],
    method: str = "pinpoint",
    alpha: float | None = None,
) -> list[dict]:
    """Mark rows as significant based on their p-values.

    method:
      "pinpoint" — per-metric threshold at α=0.01, matching the Pinpoint UI.
      "fdr"      — Benjamini-Hochberg FDR correction at α=0.05.

    alpha overrides the default for either method.
    """
    import math

    if not rows:
        return rows

    if method == "fdr":
        alpha = alpha or 0.05
        # Separate valid and NaN p-values (NaN crashes false_discovery_control)
        valid = [(i, r) for i, r in enumerate(rows) if not math.isnan(r["p_value"])]
        for r in rows:
            if math.isnan(r["p_value"]):
                r["p_value"] = 1.0
                r["significant"] = False

        if not valid:
            return rows

        raw_ps = [r["p_value"] for _, r in valid]
        adjusted = false_discovery_control(raw_ps, method="bh")
        for (_, r), adj_p in zip(valid, adjusted):
            r["p_value"] = float(adj_p)
            r["significant"] = bool(adj_p < alpha)
    else:
        alpha = alpha or 0.01
        for r in rows:
            p = r["p_value"]
            if math.isnan(p):
                r["p_value"] = 1.0
                r["significant"] = False
            else:
                r["significant"] = bool(p < alpha)
    return rows


def pivot_results(job_id: str, significance: str = "pinpoint") -> list[dict]:
    """Return one row per metric comparing base vs experiment.

    Each row has: name, unit, base_label, base_mean, base_stdev, base_n,
    exp_label, exp_mean, exp_stdev, exp_n, p_value, significant.

    Labels with "base:"/"exp:" prefix are assigned accordingly; otherwise
    alphabetical order is used. Mann-Whitney U (two-sided).

    significance: "pinpoint" (per-metric α=0.01, matches Pinpoint UI)
                  or "fdr" (Benjamini-Hochberg FDR correction, α=0.05).
    Only metrics with exactly two labels are included.
    """
    from . import pinpoint_cache

    cached = pinpoint_cache.get_results(job_id, source="histogram")
    if cached is not None:
        return cached
    histograms, guids = fetch_histograms(job_id)
    groups = _collect_groups(histograms, guids)

    by_metric: dict[str, dict[str, dict]] = defaultdict(dict)
    for (name, label), info in groups.items():
        by_metric[name][label] = info

    rows = []
    for name, by_label in sorted(by_metric.items()):
        if len(by_label) != 2:
            continue
        label_a, label_b = sorted(by_label)
        # Prefer explicit "base:"/"exp:" prefix; fall back to alphabetical order.
        if label_a.startswith("base:") or not label_b.startswith("base:"):
            base_label, exp_label = label_a, label_b
        else:
            base_label, exp_label = label_b, label_a

        base_vals = by_label[base_label]["values"]
        exp_vals = by_label[exp_label]["values"]
        p = float(mannwhitneyu(base_vals, exp_vals, alternative="two-sided").pvalue)

        rows.append(
            {
                "name": name,
                "unit": by_label[base_label]["unit"],
                "base_label": base_label,
                **{f"base_{k}": v for k, v in _value_stats(base_vals).items()},
                "exp_label": exp_label,
                **{f"exp_{k}": v for k, v in _value_stats(exp_vals).items()},
                "p_value": p,
            }
        )
    rows = _apply_significance(rows, method=significance)
    if rows:
        pinpoint_cache.put_results(job_id, rows, source="histogram")
    return rows


def fetch_raw_values(job_id: str) -> list[dict]:
    """Return per-run measurement values for a Pinpoint job.

    One row per (metric, bot run): metric, label, run_id, unit, value.
    run_id is a GUID shared across all metrics within a run (join key).
    """
    histograms, guids = fetch_histograms(job_id)
    rows = []
    for h in histograms:
        diag = h.get("diagnostics", {})
        label_guid = diag.get("labels", "unknown")
        label = guids.get(label_guid, label_guid)
        for value in h.get("sampleValues", []):
            rows.append(
                {
                    "metric": h["name"],
                    "label": label,
                    "run_id": label_guid,
                    "unit": h["unit"],
                    "value": value,
                }
            )
    return rows


# ── CAS data access ───────────────────────────────────────────────────────────


def fetch_job_state(job_id: str) -> list[dict]:
    """Return the job's 'state' list (base/experiment variants with attempts)."""
    r = httpx.get(
        f"{_PINPOINT_BASE}/api/job/{job_id}?o=STATE",
        follow_redirects=True,
        timeout=120,
    )
    r.raise_for_status()
    return r.json().get("state", [])


def _extract_cas_digests(state: list[dict]) -> tuple[list[str], list[str]]:
    """Return (base_digests, exp_digests) from job state.

    state[0] = base variant, state[1] = experiment.
    Each attempt's CAS digest lives at executions[1].details[key="isolate"].
    """

    def _digests(variant: dict) -> list[str]:
        out = []
        for attempt in variant.get("attempts", []):
            execs = attempt.get("executions", [])
            if len(execs) < 2:
                continue
            for detail in execs[1].get("details", []):
                if detail.get("key") == "isolate" and detail.get("value"):
                    out.append(detail["value"])
                    break
        return out

    base = _digests(state[0]) if len(state) > 0 else []
    exp = _digests(state[1]) if len(state) > 1 else []
    return base, exp


_BENCHMARK_TO_PROBE: dict[str, str] = {
    "jetstream-main.crossbench": "jetstream_main.json",
    "jetstream2.crossbench": "jetstream2.json",
}


def _parse_perf_results(raw: bytes) -> tuple[list[dict], dict] | None:
    """Parse a perf_results.json blob (Chromium histogram JSON array format).

    Returns (histograms, guids) in the same format as fetch_histograms(),
    or None if unparseable.
    """
    try:
        entries = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    guids = {
        e["guid"]: e["values"][0] if len(e["values"]) == 1 else e["values"]
        for e in entries
        if e.get("type") == "GenericSet"
    }
    histograms = [e for e in entries if "name" in e and "unit" in e]
    return (histograms, guids) if histograms else None


def _parse_crossbench_probe(raw: bytes) -> dict[str, list[float]] | None:
    """Parse a crossbench probe JSON (e.g. jetstream_main.json).

    Structure: {browser: {data: {"story/SubMetric": {values: [float, ...]}}}}
    Returns {story/SubMetric: [float values]} or None if unparseable.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    result: dict[str, list[float]] = {}
    for browser_data in data.values():
        if not isinstance(browser_data, dict):
            continue
        for key, entry in browser_data.get("data", {}).items():
            vals = [
                float(v) for v in entry.get("values", []) if isinstance(v, (int, float))
            ]
            if vals:
                result[key] = vals
    return result or None


def pivot_results_cas(job_id: str, significance: str = "pinpoint") -> list[dict]:
    """Like pivot_results, but fetches raw per-run values from CAS isolates.

    Uses the RBE REST API directly — no `cas` binary required.

    For benchmarks with a known probe file (e.g. JetStream → jetstream_main.json),
    returns sub-metrics (Score, First, Average, Worst4) per story.
    Falls back to aggregate metrics from perf_results.json for other benchmarks.

    Requires: gcloud auth application-default login
    """
    from . import cas_api, pinpoint_cache

    cached = pinpoint_cache.get_results(job_id, source="cas")
    if cached is not None:
        return cached

    job = fetch_job(job_id)
    if job.get("status") != "Completed":
        raise ValueError(
            f"Job is not completed (status: {job.get('status', 'Unknown')})"
        )

    benchmark = job.get("arguments", {}).get("benchmark", "")
    probe_filename = _BENCHMARK_TO_PROBE.get(benchmark)
    if not probe_filename:
        raise ValueError(
            f"No CAS probe file known for benchmark {benchmark!r}. "
            "Update _BENCHMARK_TO_PROBE in pinpoint.py to add support."
        )

    state = fetch_job_state(job_id)
    base_digests, exp_digests = _extract_cas_digests(state)
    if not base_digests or not exp_digests:
        raise ValueError("No CAS digests found in job state.")

    all_digests = base_digests + exp_digests
    n_base = len(base_digests)

    try:
        blobs = cas_api.fetch_probe_files(
            all_digests, ["perf_results.json", probe_filename]
        )
        perf_blobs = blobs["perf_results.json"]
        probe_blobs = blobs[probe_filename]
    except PermissionError as e:
        raise PermissionError(
            "CAS authentication failed.\n"
            "Ensure you are logged in:  gcloud auth application-default login\n"
            f"Details: {e}"
        ) from e

    # Extract labels and units from perf_results.json
    base_label: str | None = None
    exp_label: str | None = None
    units: dict[str, str] = {}

    for i, raw in enumerate(perf_blobs):
        if raw is None:
            continue
        parsed = _parse_perf_results(raw)
        if not parsed:
            continue
        histograms, guids = parsed
        is_base = i < n_base
        for h in histograms:
            if h.get("unit"):
                units[h["name"]] = h["unit"]
        if histograms and (base_label is None or exp_label is None):
            label_guid = histograms[0].get("diagnostics", {}).get("labels")
            label = guids.get(label_guid) if label_guid else None
            if label:
                if is_base and base_label is None:
                    base_label = label
                elif not is_base and exp_label is None:
                    exp_label = label

    # Collect values: prefer sub-metrics from probe file, fall back to perf_results
    sub_values: dict[str, dict[bool, list[float]]] = defaultdict(
        lambda: {True: [], False: []}
    )
    has_probe_data = False

    n_found = sum(1 for raw in probe_blobs if raw is not None)
    for i, raw in enumerate(probe_blobs):
        if raw is None:
            continue
        parsed_probe = _parse_crossbench_probe(raw)
        if not parsed_probe:
            continue
        has_probe_data = True
        is_base = i < n_base
        for key, vals in parsed_probe.items():
            sub_values[key][is_base].extend(vals)

    if not has_probe_data:
        if n_found == 0:
            raise ValueError(
                f"Probe file {probe_filename!r} not found in any CAS isolate tree. "
                "The benchmark directory structure may have changed."
            )
        raise ValueError(
            f"Probe file {probe_filename!r} was fetched from {n_found} isolate(s) "
            "but could not be parsed. "
            "The file structure may differ from the expected crossbench probe format "
            "{browser: {data: {story/metric: {values: [...]}}}}. "
            "Run: cat <isolate>/output/jetstream_main.json | python3 -m json.tool | head -30"
        )

    rows = []
    for name, by_side in sorted(sub_values.items()):
        base_vals = by_side[True]
        exp_vals = by_side[False]
        if not base_vals or not exp_vals:
            continue
        # For sub-metrics like "story/SubMetric", look up "story" in units dict
        story = name.rsplit("/", 1)[0] if "/" in name else name
        unit = units.get(name) or units.get(story)
        p = float(mannwhitneyu(base_vals, exp_vals, alternative="two-sided").pvalue)
        rows.append(
            {
                "name": name,
                "unit": unit,
                "base_label": base_label or "base",
                **{f"base_{k}": v for k, v in _value_stats(base_vals).items()},
                "exp_label": exp_label or "exp",
                **{f"exp_{k}": v for k, v in _value_stats(exp_vals).items()},
                "p_value": p,
            }
        )
    rows = _apply_significance(rows, method=significance)
    if rows:
        pinpoint_cache.put_results(job_id, rows, source="cas")
    return rows


# ── Build lookup ──────────────────────────────────────────────────────────────


def fetch_latest_build_commit(configuration: str) -> tuple[str, int]:
    """Return (commit_hash, build_number) for the most recent successful CI build.

    Uses Pinpoint's /api/builds/<configuration> endpoint, which resolves the
    configuration to its compile builder and queries Buildbucket.

    Raises ValueError if no builds are found or auth fails.
    """
    configuration = CONFIGURATION_ALIASES.get(configuration, configuration)
    headers = get_auth_headers()
    if not headers:
        raise ValueError(_LOGIN_INSTRUCTIONS)
    r = httpx.get(
        f"{_PINPOINT_BASE}/api/builds/{configuration}",
        headers=headers,
        follow_redirects=True,
        timeout=15,
    )
    r.raise_for_status()
    builds = r.json().get("builds", [])
    if not builds:
        raise ValueError(f"No recent builds found for configuration {configuration!r}")
    b = builds[0]
    commit = b.get("input", {}).get("gitilesCommit", {}).get("id", "")
    number = b.get("number", 0)
    if not commit:
        raise ValueError(f"Build {b.get('id')} has no gitilesCommit")
    return commit, number


# ── Job creation ──────────────────────────────────────────────────────────────

BENCHMARK_ALIASES: dict[str, tuple[str, str | None]] = {
    # alias: (full benchmark name, default story)
    "js3": ("jetstream-main.crossbench", "JetStream"),
    "js2": ("jetstream2.crossbench", "JetStream2"),
    "sp3": ("speedometer3.crossbench", "Speedometer3"),
}

CONFIGURATION_ALIASES: dict[str, str] = {
    "linux": "linux-r350-perf",
    "m1": "mac-m1_mini_2020-perf",
    "m2": "mac-m2-pro-perf",
    "m3": "mac-m3-pro-perf",
    "m4": "mac-m4-mini-perf",
    "macm4": "mac-m4-mini-perf",  # kept for backwards compatibility
}


def short_configuration(name: str) -> str:
    """Return the shortest alias for a configuration name, or the name itself."""
    matches = [k for k, v in CONFIGURATION_ALIASES.items() if v == name]
    return min(matches, key=len) if matches else name


def short_benchmark(name: str) -> str:
    """Return the alias for a benchmark name, or the name itself."""
    for alias, (full, _) in BENCHMARK_ALIASES.items():
        if full == name:
            return alias
    return name


def cancel_job(job_url: str, reason: str = "Cancelled") -> dict:
    """Cancel a Pinpoint job. Requires luci-auth login."""
    job_id = job_id_from_url(job_url)
    headers = get_auth_headers()
    if not headers:
        raise ValueError(_LOGIN_INSTRUCTIONS)
    r = httpx.post(
        f"{_PINPOINT_BASE}/api/job/cancel",
        data={"job_id": job_id, "reason": reason},
        headers=headers,
        follow_redirects=True,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def create_job(
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
    name: str | None = None,
) -> dict:
    """Create a new Pinpoint A/B try job. Requires luci-auth login."""
    if benchmark in BENCHMARK_ALIASES:
        benchmark, default_story = BENCHMARK_ALIASES[benchmark]
        if story is None:
            story = default_story

    configuration = CONFIGURATION_ALIASES.get(configuration, configuration)

    payload = {
        "comparison_mode": "try",
        "benchmark": benchmark,
        "configuration": configuration,
        "story": story,
        "story_tags": story_tags,
        "initial_attempt_count": str(repeat),
        "bug_id": bug_id,
        "base_git_hash": base_git_hash,
        "end_git_hash": exp_git_hash,
        "base_patch": resolve_patch(base_patch) if base_patch else None,
        "experiment_patch": resolve_patch(exp_patch) if exp_patch else None,
        "base_extra_args": f'--js-flags="{base_js_flags}"' if base_js_flags else None,
        "experiment_extra_args": f'--js-flags="{exp_js_flags}"'
        if exp_js_flags
        else None,
        "tags": '{"origin": "v8-utils"}',
        "name": name,
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    headers = get_auth_headers()
    if not headers:
        raise ValueError(_LOGIN_INSTRUCTIONS)
    # Prefer chromium.org if available (get_current_user_email already resolves the preference)
    try:
        email = get_current_user_email()
        if not email.endswith("@google.com"):
            alt = get_auth_headers(email)
            if alt:
                headers = alt
    except Exception:
        pass

    r = httpx.post(
        f"{_PINPOINT_BASE}/api/new",
        data=payload,
        headers=headers,
        follow_redirects=True,
        timeout=30,
    )
    r.raise_for_status()
    result = r.json()
    job_id = result.get("jobId") or result.get("job_id")
    if job_id:
        result["url"] = f"{_PINPOINT_BASE}/job/{job_id}"
    return result
