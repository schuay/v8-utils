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


def _fetch_results(job_id: str) -> list[dict]:
    """Fetch and parse the histogram results for a Pinpoint job.

    Returns a list of dicts, one per (metric_name, label), each with:
      name, unit, label, n, mean, stdev, min, max
    Labels are the resolved human-readable strings (e.g. "base: ..." / "exp: ...").
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

    # Build guid -> value map from GenericSet entries.
    guids = {
        e["guid"]: e["values"][0] if len(e["values"]) == 1 else e["values"]
        for e in entries
        if e.get("type") == "GenericSet"
    }

    # Group sample values by (metric_name, label).
    groups: dict[tuple[str, str], dict] = defaultdict(lambda: {"unit": None, "values": []})
    for e in entries:
        if "name" not in e or "unit" not in e:
            continue
        diag = e.get("diagnostics", {})
        label = guids.get(diag.get("labels"), diag.get("labels", "unknown"))
        key = (e["name"], label)
        groups[key]["unit"] = e["unit"]
        groups[key]["values"].extend(e.get("sampleValues", []))

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
