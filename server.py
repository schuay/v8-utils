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


def _fetch_histograms(job_id: str) -> tuple[list[dict], dict[str, str]]:
    """Fetch and parse the raw histogram entries for a Pinpoint job.

    Returns (histograms, guids) where:
      histograms  list of raw histogram dicts (entries with name + unit)
      guids       mapping from GUID string to resolved scalar value
    """
    job = _fetch_job(job_id)
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
      name, unit, label, n, mean, stdev, min, max, p_value, significant
    Labels are the resolved human-readable strings (e.g. "base: ..." / "exp: ...").
    p_value and significant are set when exactly two labels exist for a metric
    (Mann-Whitney U test, two-sided, alpha=0.05).
    """
    histograms, guids = _fetch_histograms(job_id)

    groups: dict[tuple[str, str], dict] = defaultdict(lambda: {"unit": None, "values": []})
    for h in histograms:
        diag = h.get("diagnostics", {})
        label = guids.get(diag.get("labels"), diag.get("labels", "unknown"))
        key = (h["name"], label)
        groups[key]["unit"] = h["unit"]
        groups[key]["values"].extend(h.get("sampleValues", []))

    # Compute per-metric Mann-Whitney U p-values when there are exactly 2 labels.
    by_metric: dict[str, dict[str, list]] = defaultdict(dict)
    for (name, label), info in groups.items():
        by_metric[name][label] = info["values"]

    p_values: dict[tuple[str, str], float | None] = {}
    for name, by_label in by_metric.items():
        if len(by_label) == 2:
            (label_a, vals_a), (label_b, vals_b) = by_label.items()
            result = mannwhitneyu(vals_a, vals_b, alternative="two-sided")
            p_values[(name, label_a)] = result.pvalue
            p_values[(name, label_b)] = result.pvalue
        else:
            for label in by_label:
                p_values[(name, label)] = None

    results = []
    for (name, label), info in sorted(groups.items()):
        vals = info["values"]
        p = p_values.get((name, label))
        results.append({
            "name":        name,
            "label":       label,
            "unit":        info["unit"],
            "n":           len(vals),
            "mean":        statistics.mean(vals) if vals else None,
            "stdev":       statistics.stdev(vals) if len(vals) > 1 else None,
            "min":         min(vals) if vals else None,
            "max":         max(vals) if vals else None,
            "p_value":     p,
            "significant": (p < 0.05) if p is not None else None,
        })
    return results


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
def pinpoint_show_results(job_url: str) -> list[dict]:
    """Fetch and display all histogram results for a Pinpoint job.

    Returns one entry per (metric, label) with name, unit, label, n, mean,
    stdev, min, and max.  Labels identify the base and experiment variants.

    job_url: Pinpoint job URL, e.g.
             https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000
    """
    job_id = _job_id_from_url(job_url)
    return _fetch_results(job_id)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
