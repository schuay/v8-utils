"""MCP server exposing tools useful for V8 JavaScript engine developers.

Run directly:  python server.py
Or via MCP CLI: mcp run server.py

Note the server may be upgraded via: uv tool upgrade v8-mcp
"""

import re

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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
