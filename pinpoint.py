"""Pinpoint performance infrastructure — data access and processing helpers."""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from collections import defaultdict
from typing import Any

import httpx
from scipy.stats import mannwhitneyu

_PINPOINT_BASE = "https://pinpoint-dot-chromeperf.appspot.com"
_GERRIT_BASE = "https://chromium-review.googlesource.com"

_LOGIN_INSTRUCTIONS = (
    "Not logged in via luci-auth. "
    "Run:  luci-auth login -scopes https://www.googleapis.com/auth/userinfo.email"
)


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
        token = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
        return {"Authorization": f"Bearer {token}"}
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}


def user_email_variants(email: str) -> list[str]:
    """Return email plus its @google.com and @chromium.org counterparts."""
    username = email.split("@")[0]
    variants = [email, f"{username}@google.com", f"{username}@chromium.org"]
    return list(dict.fromkeys(variants))  # deduplicate, preserve order


# ── Gerrit patch resolver ──────────────────────────────────────────────────────

_RE_CRREV = re.compile(
    r"^(?:https?://)?(?:crrev(?:\.com)?/)?(?:c/)?(\d+)(?:/(\d+))?$", re.IGNORECASE
)


def resolve_patch(patch: str) -> str:
    """Resolve a Gerrit patch shorthand to a full chromium-review URL.

    Accepts: bare change ID (12345), crrev/c/12345, crrev.com/c/12345,
             or a full https://chromium-review.googlesource.com/... URL.
    """
    patch = patch.strip()
    m = _RE_CRREV.match(patch)
    if m:
        change_id, patchset = m.group(1), m.group(2)
        r = httpx.get(f"{_GERRIT_BASE}/changes/{change_id}", timeout=15)
        r.raise_for_status()
        text = r.text[r.text.find("{"):]  # strip Gerrit's XSSI prefix ")]}'"
        project = json.loads(text)["project"]
        url = f"{_GERRIT_BASE}/c/{project}/+/{change_id}"
        return f"{url}/{patchset}" if patchset else url

    if patch.startswith("http"):
        return patch

    raise ValueError(
        f"Unrecognised patch format: {patch!r}. "
        "Expected a change ID (12345), crrev/c/12345, or a full Gerrit URL."
    )


_RE_GERRIT_CHANGE_ID = re.compile(r"/(\d+)(?:/\d+)?/?$")


def fetch_gerrit_subject(patch_url: str) -> str | None:
    """Return the subject (first line of commit message) for a Gerrit change URL.

    Returns None if the change ID cannot be extracted or the request fails.
    """
    m = _RE_GERRIT_CHANGE_ID.search(patch_url)
    if not m:
        return None
    try:
        r = httpx.get(f"{_GERRIT_BASE}/changes/{m.group(1)}", timeout=15)
        r.raise_for_status()
        text = r.text[r.text.find("{"):]
        return json.loads(text).get("subject")
    except Exception:
        return None


# ── Job listing ───────────────────────────────────────────────────────────────

def job_id_from_url(job_url: str) -> str:
    """Extract the job ID from a Pinpoint job URL, or return the input unchanged."""
    m = re.search(r"/job/([a-zA-Z0-9]+)", job_url)
    return m.group(1) if m else job_url


def fetch_job(job_id: str) -> dict[str, Any]:
    """Fetch raw job JSON from the Pinpoint API."""
    r = httpx.get(f"{_PINPOINT_BASE}/api/job/{job_id}", follow_redirects=True, timeout=30)
    r.raise_for_status()
    return r.json()


def _is_cq_job(job: dict) -> bool:
    tags_raw = job.get("arguments", {}).get("tags", "")
    try:
        tags = json.loads(tags_raw) if tags_raw else {}
    except (ValueError, TypeError):
        tags = {}
    return tags.get("origin") == "CQ"


def _job_matches_filter(job: dict, filter_str: str) -> bool:
    """Test a job against a "key=value" filter string (case-insensitive substring).

    Supported keys: status, benchmark, configuration, comparison_mode.
    """
    if "=" not in filter_str:
        return True
    key, _, value = filter_str.partition("=")
    key, value = key.strip().lower(), value.strip().lower()
    args = job.get("arguments", {})
    field = {
        "status":          job.get("status", ""),
        "benchmark":       args.get("benchmark", ""),
        "configuration":   job.get("configuration", ""),
        "comparison_mode": job.get("comparison_mode", ""),
    }.get(key, "")
    return value in field.lower()


def _fetch_jobs_for_email(email: str, count: int, extra_filter: str | None) -> list[dict]:
    """Fetch up to `count` non-CQ jobs for a single email via the Pinpoint API."""
    matched: list[dict] = []
    seen_ids: set[str] = set()
    params: dict = {"filter": f"user={email}"}

    while len(matched) < count:
        r = httpx.get(
            f"{_PINPOINT_BASE}/api/jobs", params=params,
            follow_redirects=True, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        page = [j for j in data.get("jobs", []) if not _is_cq_job(j)]
        if extra_filter:
            page = [j for j in page if _job_matches_filter(j, extra_filter)]
        for j in page:
            if j["job_id"] not in seen_ids:
                seen_ids.add(j["job_id"])
                matched.append(j)
        next_cursor = data.get("next_cursor")
        if not data.get("next") or not next_cursor or next_cursor == params.get("next_cursor"):
            break
        params["next_cursor"] = next_cursor

    return matched


def fetch_jobs(user: str, count: int, filter_str: str | None = None) -> list[dict]:
    """Fetch the `count` most recent non-CQ jobs for a user.

    Queries all email variants (@google.com, @chromium.org) and merges.
    The /api/jobs endpoint is public; no auth required.
    """
    seen_ids: set[str] = set()
    all_jobs: list[dict] = []
    for jobs in [_fetch_jobs_for_email(e, count, filter_str) for e in user_email_variants(user)]:
        for j in jobs:
            if j["job_id"] not in seen_ids:
                seen_ids.add(j["job_id"])
                all_jobs.append(j)
    all_jobs.sort(key=lambda j: j.get("created", ""), reverse=True)
    return all_jobs[:count]


def summarise_job(j: dict) -> dict:
    """Extract the key fields from a raw job dict."""
    args = j.get("arguments", {})
    return {
        "job_id":                j.get("job_id"),
        "url":                   f"{_PINPOINT_BASE}/job/{j.get('job_id')}",
        "name":                  j.get("name"),
        "status":                j.get("status"),
        "created":               j.get("created"),
        "configuration":         j.get("configuration"),
        "benchmark":             args.get("benchmark"),
        "story":                 args.get("story"),
        "base_git_hash":         args.get("base_git_hash"),
        "experiment_patch":      args.get("experiment_patch"),
        "base_extra_args":       args.get("base_extra_args"),
        "experiment_extra_args": args.get("experiment_extra_args"),
        "difference_count":      j.get("difference_count"),
        "exception":             j.get("exception"),
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
    data_block = next((c for c in reversed(comments) if c.lstrip().startswith("{")), None)
    if not data_block:
        raise ValueError("Could not find histogram data block in results page")

    entries = [json.loads(line) for line in data_block.splitlines() if line.strip()]
    guids = {
        e["guid"]: e["values"][0] if len(e["values"]) == 1 else e["values"]
        for e in entries if e.get("type") == "GenericSet"
    }
    histograms = [e for e in entries if "name" in e and "unit" in e]
    return histograms, guids


def _collect_groups(histograms: list[dict], guids: dict) -> dict[tuple[str, str], dict]:
    """Group histogram sample values by (metric_name, label)."""
    groups: dict[tuple[str, str], dict] = defaultdict(lambda: {"unit": None, "values": []})
    for h in histograms:
        diag = h.get("diagnostics", {})
        label = guids.get(diag.get("labels"), diag.get("labels", "unknown"))
        key = (h["name"], label)
        groups[key]["unit"] = h["unit"]
        groups[key]["values"].extend(h.get("sampleValues", []))
    return groups


def _value_stats(vals: list[float]) -> dict:
    return {
        "mean":  statistics.mean(vals) if vals else None,
        "stdev": statistics.stdev(vals) if len(vals) > 1 else None,
        "n":     len(vals),
    }


def pivot_results(job_id: str) -> list[dict]:
    """Return one row per metric comparing base vs experiment.

    Each row has: name, unit, base_label, base_mean, base_stdev, base_n,
    exp_label, exp_mean, exp_stdev, exp_n, p_value, significant.

    Labels with "base:"/"exp:" prefix are assigned accordingly; otherwise
    alphabetical order is used. Mann-Whitney U (two-sided, α=0.05).
    Only metrics with exactly two labels are included.
    """
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
        exp_vals  = by_label[exp_label]["values"]
        p = float(mannwhitneyu(base_vals, exp_vals, alternative="two-sided").pvalue)

        rows.append({
            "name":        name,
            "unit":        by_label[base_label]["unit"],
            "base_label":  base_label,
            **{f"base_{k}": v for k, v in _value_stats(base_vals).items()},
            "exp_label":   exp_label,
            **{f"exp_{k}":  v for k, v in _value_stats(exp_vals).items()},
            "p_value":     p,
            "significant": bool(p < 0.05),
        })
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
            rows.append({
                "metric": h["name"],
                "label":  label,
                "run_id": label_guid,
                "unit":   h["unit"],
                "value":  value,
            })
    return rows


# ── CAS data access ───────────────────────────────────────────────────────────

# Maps Pinpoint benchmark name → crossbench probe filename (without .json).
# The probe file is searched by name anywhere in the isolate tree, so this
# works regardless of which subdirectory the benchmark places it in.
_BENCHMARK_TO_PROBE: dict[str, str] = {
    "jetstream-main.crossbench": "jetstream_main",
    "jetstream2.crossbench":     "jetstream_2.2",
    "speedometer3.crossbench":   "speedometer_main",
}


def fetch_job_state(job_id: str) -> list[dict]:
    """Return the job's 'state' list (base/experiment variants with attempts)."""
    r = httpx.get(
        f"{_PINPOINT_BASE}/api/job/{job_id}?o=STATE",
        follow_redirects=True, timeout=120,
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
    exp  = _digests(state[1]) if len(state) > 1 else []
    return base, exp


def _parse_probe_json(raw: bytes) -> dict[str, float] | None:
    """Parse a crossbench probe JSON blob into a flat {metric_key: float} dict.

    Expected structure: {browser_label: {data: {"story/Metric": {values: [float]}}}}
    Returns the mean of each metric's values list, or None if unparseable.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    result: dict[str, float] = {}
    for browser_data in data.values():
        if not isinstance(browser_data, dict):
            continue
        for metric_key, stats in browser_data.get("data", {}).items():
            if not isinstance(stats, dict):
                continue
            vals = stats.get("values", [])
            if vals:
                result[metric_key] = sum(vals) / len(vals)
    return result or None


def pivot_results_cas(job_id: str) -> list[dict]:
    """Like pivot_results, but fetches raw per-run values from CAS isolates.

    Uses the RBE REST API directly — no `cas` binary required.  Walks each
    isolate's directory tree to find the probe JSON by filename, then
    batch-fetches all file blobs in as few API calls as possible.

    For JetStream this surfaces Score, First, Average, and Worst4 per story
    rather than just the headline Score from the histogram HTML.

    Requires: gcloud auth application-default login
    """
    import cas_api

    job = fetch_job(job_id)
    status = job.get("status", "Unknown")
    if status != "Completed":
        raise ValueError(f"Job is not completed (status: {status})")

    benchmark = job.get("arguments", {}).get("benchmark", "")
    probe_name = _BENCHMARK_TO_PROBE.get(benchmark)
    if not probe_name:
        raise ValueError(
            f"CAS data access is not supported for benchmark {benchmark!r}.\n"
            f"Supported: {', '.join(_BENCHMARK_TO_PROBE)}"
        )

    state = fetch_job_state(job_id)
    base_digests, exp_digests = _extract_cas_digests(state)
    if not base_digests or not exp_digests:
        raise ValueError("No CAS digests found in job state.")

    # Fetch the full labels (e.g. "base: chromium@abc123 (...)") from histograms.
    # Fall back to the short state labels if the results page isn't available yet.
    try:
        _histograms, _guids = fetch_histograms(job_id)
        _groups = _collect_groups(_histograms, _guids)
        _labels = sorted({label for _, label in _groups})
        base_label = next((l for l in _labels if l.startswith("base:")), _labels[0] if _labels else "base")
        exp_label  = next((l for l in _labels if l.startswith("exp:")),  _labels[1] if len(_labels) > 1 else "exp")
    except Exception:
        base_label = (state[0].get("change", {}).get("label") or "base") if state else "base"
        exp_label  = (state[1].get("change", {}).get("label") or "exp")  if len(state) > 1 else "exp"

    all_digests = base_digests + exp_digests
    probe_filename = f"{probe_name}.json"

    try:
        blobs = cas_api.fetch_probe_files(all_digests, probe_filename)
    except PermissionError as e:
        raise PermissionError(
            f"CAS authentication failed.\n"
            f"Ensure you are logged in:  gcloud auth application-default login\n"
            f"Details: {e}"
        ) from e

    # {"story/Metric": {True: [base floats], False: [exp floats]}}
    values: dict[str, dict[bool, list[float]]] = defaultdict(lambda: {True: [], False: []})

    for i, raw in enumerate(blobs):
        if raw is None:
            continue
        is_base = i < len(base_digests)
        parsed = _parse_probe_json(raw)
        if parsed is None:
            continue
        for metric_key, val in parsed.items():
            if isinstance(val, (int, float)):
                values[metric_key][is_base].append(float(val))

    if not values:
        raise ValueError(
            f"No probe data found in CAS isolates. "
            f"Looked for {probe_filename!r} anywhere in each isolate tree."
        )

    rows = []
    for metric_key, by_side in sorted(values.items()):
        base_vals = by_side[True]
        exp_vals  = by_side[False]
        if not base_vals or not exp_vals:
            continue
        p = float(mannwhitneyu(base_vals, exp_vals, alternative="two-sided").pvalue)
        rows.append({
            "name":        metric_key,
            "unit":        None,
            "base_label":  base_label,
            **{f"base_{k}": v for k, v in _value_stats(base_vals).items()},
            "exp_label":   exp_label,
            **{f"exp_{k}":  v for k, v in _value_stats(exp_vals).items()},
            "p_value":     p,
            "significant": bool(p < 0.05),
        })
    return rows


# ── Job creation ──────────────────────────────────────────────────────────────

BENCHMARK_ALIASES: dict[str, tuple[str, str | None]] = {
    # alias: (full benchmark name, default story)
    "js3": ("jetstream-main.crossbench", "JetStream"),
    "js2": ("jetstream2.crossbench",     "JetStream2"),
    "sp3": ("speedometer3.crossbench",   "Speedometer3"),
}

CONFIGURATION_ALIASES: dict[str, str] = {
    "linux": "linux-r350-perf",
    "m1":    "mac-m1_mini_2020-perf",
    "m3":    "mac-m3-pro-perf",
    "m4":    "mac-m4-mini-perf",
    "macm4": "mac-m4-mini-perf",   # kept for backwards compatibility
}


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
) -> dict:
    """Create a new Pinpoint A/B try job. Requires luci-auth login."""
    if benchmark in BENCHMARK_ALIASES:
        benchmark, default_story = BENCHMARK_ALIASES[benchmark]
        if story is None:
            story = default_story

    configuration = CONFIGURATION_ALIASES.get(configuration, configuration)

    payload = {
        "comparison_mode":       "try",
        "benchmark":             benchmark,
        "configuration":         configuration,
        "story":                 story,
        "story_tags":            story_tags,
        "initial_attempt_count": str(repeat),
        "bug_id":                bug_id,
        "base_git_hash":         base_git_hash,
        "end_git_hash":          exp_git_hash,
        "base_patch":            resolve_patch(base_patch) if base_patch else None,
        "experiment_patch":      resolve_patch(exp_patch) if exp_patch else None,
        "base_extra_args":       f'--js-flags="{base_js_flags}"' if base_js_flags else None,
        "experiment_extra_args": f'--js-flags="{exp_js_flags}"' if exp_js_flags else None,
        "tags":                  '{"origin": "v8-utils"}',
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
        f"{_PINPOINT_BASE}/api/new", data=payload,
        headers=headers, follow_redirects=True, timeout=30,
    )
    r.raise_for_status()
    result = r.json()
    job_id = result.get("jobId") or result.get("job_id")
    if job_id:
        result["url"] = f"{_PINPOINT_BASE}/job/{job_id}"
    return result
