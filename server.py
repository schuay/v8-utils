"""MCP server exposing tools useful for V8 JavaScript engine developers.

Run directly:  python server.py
Or via MCP CLI: mcp run server.py

Note the server may be upgraded via: uv tool upgrade v8-mcp
"""

import json
import re
import statistics
from collections import defaultdict

import httpx
from mcp.server.fastmcp import FastMCP
from scipy.stats import mannwhitneyu

mcp = FastMCP("v8-mcp")

_PINPOINT_BASE = "https://pinpoint-dot-chromeperf.appspot.com"


# ── Pinpoint helpers ──────────────────────────────────────────────────────────

def _job_id_from_url(job_url: str) -> str:
    """Extract the job ID from a Pinpoint job URL or return it unchanged."""
    m = re.search(r"/job/([a-zA-Z0-9]+)", job_url)
    return m.group(1) if m else job_url


def _fetch_job(job_id: str) -> dict:
    """Fetch raw job JSON from the Pinpoint API."""
    url = f"{_PINPOINT_BASE}/api/job/{job_id}"
    r = httpx.get(url, follow_redirects=True, timeout=30)
    r.raise_for_status()
    return r.json()


def _job_matches_filter(job: dict, filter: str) -> bool:
    """Test a job against a "key:value" filter string.

    Supported keys: status, benchmark, configuration, comparison_mode.
    Matching is case-insensitive substring on the value.
    """
    if ":" not in filter:
        return True
    key, _, value = filter.partition(":")
    key, value = key.strip().lower(), value.strip().lower()
    args = job.get("arguments", {})
    candidates = {
        "status":          job.get("status", ""),
        "benchmark":       args.get("benchmark", ""),
        "configuration":   job.get("configuration", ""),
        "comparison_mode": job.get("comparison_mode", ""),
    }
    return value in candidates.get(key, "").lower()


def _fetch_jobs(user: str, count: int, filter: str | None = None) -> list[dict]:
    """Fetch the most recent `count` jobs for a user matching an optional filter.

    Paginates the API (50 jobs/page) and applies filter client-side until
    `count` matching jobs are collected or the full history is exhausted.
    """
    matched = []
    params: dict = {"user": user}

    while len(matched) < count:
        r = httpx.get(f"{_PINPOINT_BASE}/api/jobs", params=params, follow_redirects=True, timeout=30)
        r.raise_for_status()
        data = r.json()
        page = data.get("jobs", [])
        if filter:
            page = [j for j in page if _job_matches_filter(j, filter)]
        matched.extend(page)
        if not data.get("next"):
            break
        params["cursor"] = data["next_cursor"]

    return matched[:count]


def _summarise_job(j: dict) -> dict:
    """Extract the interesting fields from a raw job dict."""
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


def _fetch_histograms(job_id: str) -> tuple[list[dict], dict[str, str]]:
    """Fetch and parse the raw histogram entries for a Pinpoint job.

    Returns (histograms, guids) where:
      histograms  list of raw histogram dicts (entries with name + unit)
      guids       mapping from GUID string to resolved scalar value
    """
    job = _fetch_job(job_id)
    status = job.get("status", "Unknown")
    if status != "Completed":
        raise ValueError(f"Job is not completed (status: {status})")

    results_path = job.get("results_url")
    if not results_path:
        raise ValueError("Job has no results_url")

    url = _PINPOINT_BASE + results_path
    r = httpx.get(url, follow_redirects=True, timeout=60)
    r.raise_for_status()

    # The histogram data is embedded as NDJSON inside the last HTML comment block.
    comments = re.findall(r"<!--(.*?)-->", r.text, re.DOTALL)
    data_block = next(
        (c for c in reversed(comments) if c.lstrip().startswith("{")),
        None,
    )
    if not data_block:
        raise ValueError("Could not find histogram data block in results page")

    entries = [json.loads(line) for line in data_block.splitlines() if line.strip()]

    guids = {
        e["guid"]: e["values"][0] if len(e["values"]) == 1 else e["values"]
        for e in entries
        if e.get("type") == "GenericSet"
    }
    histograms = [e for e in entries if "name" in e and "unit" in e]
    return histograms, guids


def _fetch_results(job_id: str) -> list[dict]:
    """Aggregate histogram results for a Pinpoint job by (metric, label).

    Returns a list of dicts, one per (metric_name, label), each with:
      name, unit, label, n, mean, stdev, min, max
    Labels are the resolved human-readable strings (e.g. "base: ..." / "exp: ...").
    """
    histograms, guids = _fetch_histograms(job_id)

    groups: dict[tuple[str, str], dict] = defaultdict(lambda: {"unit": None, "values": []})
    for h in histograms:
        diag = h.get("diagnostics", {})
        label = guids.get(diag.get("labels"), diag.get("labels", "unknown"))
        key = (h["name"], label)
        groups[key]["unit"] = h["unit"]
        groups[key]["values"].extend(h.get("sampleValues", []))

    results = []
    for (name, label), info in sorted(groups.items()):
        vals = info["values"]
        results.append({
            "name":  name,
            "label": label,
            "unit":  info["unit"],
            "n":     len(vals),
            "mean":  statistics.mean(vals) if vals else None,
            "stdev": statistics.stdev(vals) if len(vals) > 1 else None,
            "min":   min(vals) if vals else None,
            "max":   max(vals) if vals else None,
        })
    return results


def _pivot_results(job_id: str) -> list[dict]:
    """Aggregate and compare base vs experiment for a Pinpoint job.

    Returns a list of dicts, one per metric, each with:
      name, unit, base_label, base_mean, base_stdev, base_n,
      exp_label, exp_mean, exp_stdev, exp_n, p_value, significant

    Labels starting with "base:" / "exp:" are identified by prefix;
    otherwise the two labels are assigned alphabetically.
    Mann-Whitney U test (two-sided, alpha=0.05) is used for significance.
    Only metrics with exactly two labels are included.
    """
    histograms, guids = _fetch_histograms(job_id)

    # Collect values per (metric, label).
    groups: dict[tuple[str, str], dict] = defaultdict(lambda: {"unit": None, "values": []})
    for h in histograms:
        diag = h.get("diagnostics", {})
        label = guids.get(diag.get("labels"), diag.get("labels", "unknown"))
        key = (h["name"], label)
        groups[key]["unit"] = h["unit"]
        groups[key]["values"].extend(h.get("sampleValues", []))

    # Group labels per metric.
    by_metric: dict[str, dict[str, dict]] = defaultdict(dict)
    for (name, label), info in groups.items():
        by_metric[name][label] = info

    def _is_base(label: str) -> bool:
        return label.startswith("base:")

    pivoted = []
    for name, by_label in sorted(by_metric.items()):
        if len(by_label) != 2:
            continue
        label_a, label_b = sorted(by_label)
        # Prefer explicit "base:"/"exp:" prefix; fall back to alphabetical order.
        if _is_base(label_a) or not _is_base(label_b):
            base_label, exp_label = label_a, label_b
        else:
            base_label, exp_label = label_b, label_a

        base_info = by_label[base_label]
        exp_info  = by_label[exp_label]
        base_vals = base_info["values"]
        exp_vals  = exp_info["values"]

        p = float(mannwhitneyu(base_vals, exp_vals, alternative="two-sided").pvalue)

        def _stats(vals):
            return {
                "mean":  statistics.mean(vals) if vals else None,
                "stdev": statistics.stdev(vals) if len(vals) > 1 else None,
                "n":     len(vals),
            }

        pivoted.append({
            "name":        name,
            "unit":        base_info["unit"],
            "base_label":  base_label,
            **{f"base_{k}": v for k, v in _stats(base_vals).items()},
            "exp_label":   exp_label,
            **{f"exp_{k}":  v for k, v in _stats(exp_vals).items()},
            "p_value":     p,
            "significant": bool(p < 0.05),
        })
    return pivoted


def _fetch_raw_values(job_id: str) -> list[dict]:
    """Return per-run histogram values for a Pinpoint job.

    Returns a list of dicts, one per (metric, run), each with:
      metric, label, run_id, unit, value

    run_id is the label GUID, which is unique per bot run and consistent
    across all metrics within the same run, making it suitable for joining.
    """
    histograms, guids = _fetch_histograms(job_id)

    rows = []
    for h in histograms:
        diag = h.get("diagnostics", {})
        label_guid = diag.get("labels", "unknown")
        label = guids.get(label_guid, label_guid)
        for value in h.get("sampleValues", []):
            rows.append({
                "metric":  h["name"],
                "label":   label,
                "run_id":  label_guid,
                "unit":    h["unit"],
                "value":   value,
            })
    return rows


# ── Pinpoint tools ────────────────────────────────────────────────────────────

@mcp.tool()
def pinpoint_show_job(job_url: str) -> dict:
    """Fetch and display key information about a Pinpoint job.

    job_url: Pinpoint job URL, e.g.
             https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000
    """
    job_id = _job_id_from_url(job_url)
    data = _fetch_job(job_id)

    args = data.get("arguments", {})
    return {
        "job_id":             data.get("job_id"),
        "name":               data.get("name"),
        "status":             data.get("status"),
        "user":               data.get("user"),
        "created":            data.get("created"),
        "updated":            data.get("updated"),
        "comparison_mode":    data.get("comparison_mode"),
        "configuration":      data.get("configuration"),
        "benchmark":          args.get("benchmark"),
        "story":              args.get("story"),
        "base_git_hash":      args.get("base_git_hash"),
        "end_git_hash":       args.get("end_git_hash"),
        "experiment_patch":   args.get("experiment_patch"),
        "base_extra_args":    args.get("base_extra_args"),
        "experiment_extra_args": args.get("experiment_extra_args"),
        "difference_count":   data.get("difference_count"),
        "exception":          data.get("exception"),
        "bug_id":             data.get("bug_id"),
        "results_url":        data.get("results_url"),
        "bots":               data.get("bots"),
    }


@mcp.tool()
def pinpoint_list_jobs(
    user: str,
    count: int = 20,
    filter: str | None = None,
) -> list[dict]:
    """List recent Pinpoint jobs for a user, newest first.

    user:   user email, e.g. "jkummerow@chromium.org"
    count:  number of jobs to return (default 20)
    filter: optional "key:value" filter (applied client-side), e.g.:
              "status:Completed"
              "benchmark:jetstream2"
              "configuration:mac-m4"
              "comparison_mode:try"

    Each entry includes job_id, url, name, status, created, configuration,
    benchmark, story, base/experiment patch and extra_args, difference_count.
    """
    return [_summarise_job(j) for j in _fetch_jobs(user, count, filter)]


@mcp.tool()
def pinpoint_get_raw_values(job_url: str) -> list[dict]:
    """Return per-run measurement values for a Pinpoint job.

    Returns one row per (metric, bot run) with columns:
      metric, label, run_id, unit, value

    run_id is a GUID that uniquely identifies a single bot run and is
    consistent across all metrics, so rows can be joined or grouped by it.
    Suitable for downstream aggregation, statistical tests, or export.

    job_url: Pinpoint job URL, e.g.
             https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000
    """
    job_id = _job_id_from_url(job_url)
    return _fetch_raw_values(job_id)


@mcp.tool()
def pinpoint_show_results(job_url: str, show_all: bool = False) -> str:
    """Fetch and display a base-vs-experiment comparison for a Pinpoint job.

    Prints one line per metric with base mean±stdev, exp mean±stdev, %change,
    p-value, and a significance marker.  Sorted by absolute % change.

    show_all: if False (default), only show statistically significant results.

    job_url: Pinpoint job URL, e.g.
             https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000
    """
    job_id = _job_id_from_url(job_url)
    rows = _pivot_results(job_id)
    if not rows:
        return "No results found."

    if not show_all:
        rows = [r for r in rows if r["significant"]]
    if not rows:
        return "No statistically significant results found."

    def _pct(r):
        bm = r["base_mean"] or 0
        return (r["exp_mean"] - bm) / bm * 100 if bm else 0

    rows.sort(key=_pct, reverse=True)

    base_label = rows[0]["base_label"]
    exp_label  = rows[0]["exp_label"]

    # Build cell strings first so we can fit column widths to content.
    cells = []
    for r in rows:
        bm, bs = r["base_mean"] or 0, r["base_stdev"] or 0
        em, es = r["exp_mean"]  or 0, r["exp_stdev"]  or 0
        cells.append((
            r["name"],
            f"{bm:.3f} ±{bs:.3f}",
            f"{em:.3f} ±{es:.3f}",
            f"{_pct(r):+.2f}%",
            f"{r['p_value']:.4f}",
            "*" if r["significant"] else "",
        ))

    hdrs = ("metric", "base mean±stdev", "exp mean±stdev", "chg%", "p", "sig")
    widths = [max(len(h), max(len(c[i]) for c in cells)) for i, h in enumerate(hdrs)]

    def _row(cols):
        return "  ".join(c.ljust(widths[i]) if i == 0 else c.rjust(widths[i]) for i, c in enumerate(cols))

    sep = "-" * (sum(widths) + 2 * (len(widths) - 1))
    lines = [
        f"base: {base_label}",
        f"exp:  {exp_label}",
        "",
        _row(hdrs),
        sep,
        *(_row(c) for c in cells),
    ]
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
