"""Gerrit REST API tools."""

from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import quote, urlparse

import httpx

import pinpoint

_XSSI = ")]}'\n"


# ── URL parsing ───────────────────────────────────────────────────────────────


def _parse_change_url(url: str) -> tuple[str, str, str, str | None]:
    """Parse a Gerrit change URL into (api_base, project, change_id, patchset).

    Accepts:
      https://chromium-review.googlesource.com/c/v8/v8/+/7650974
      https://chromium-review.googlesource.com/c/v8/v8/+/7650974/1
      https://chromium-review.googlesource.com/7650974
      https://chromium-review.googlesource.com/7650974/1
    """
    p = urlparse(url)
    api_base = f"{p.scheme}://{p.netloc}"
    path = p.path.rstrip("/")

    m = re.match(r"^/c/(.+)/\+/(\d+)(?:/(\d+))?$", path)
    if m:
        return api_base, m.group(1), m.group(2), m.group(3)

    m = re.match(r"^/(\d+)(?:/(\d+))?$", path)
    if m:
        return api_base, "", m.group(1), m.group(2)

    raise ValueError(f"Cannot parse Gerrit change URL: {url!r}")


# ── HTTP helper ───────────────────────────────────────────────────────────────


def _get(api_base: str, path: str) -> dict | list:
    """GET against the Gerrit REST API, with auth upgrade on 401."""
    r = httpx.get(f"{api_base}{path}", timeout=30)
    if r.status_code == 401:
        headers = pinpoint.get_auth_headers()
        if headers:
            r = httpx.get(f"{api_base}/a{path}", headers=headers, timeout=30)
    r.raise_for_status()
    text = r.text
    if text.startswith(_XSSI):
        text = text[len(_XSSI) :]
    return json.loads(text)


# ── Comments ──────────────────────────────────────────────────────────────────


def comments(change_url: str) -> list[dict]:
    """Return all published comments on a CL, as a flat list of threads.

    Each thread has: file, line, patch_set, author, message, replies[].
    Threads are sorted by file then line.
    """
    api_base, project, change_id, _ = _parse_change_url(change_url)
    cid = f"{quote(project, safe='')}~{change_id}" if project else change_id
    data: dict = _get(api_base, f"/changes/{cid}/comments")

    # Build id → comment map
    by_id: dict[str, dict] = {}
    for filepath, cs in data.items():
        for c in cs:
            c["_file"] = filepath
            by_id[c["id"]] = c

    # Root comments only; build thread for each
    def _thread(root: dict) -> dict:
        replies = sorted(
            [c for c in by_id.values() if c.get("in_reply_to") == root["id"]],
            key=lambda c: c.get("updated", ""),
        )
        return {
            "file": root["_file"],
            "line": root.get("line"),
            "patch_set": root.get("patch_set"),
            "unresolved": root.get("unresolved", False),
            "author": root.get("author", {}).get("email", "unknown"),
            "message": root.get("message", ""),
            "updated": root.get("updated", ""),
            "replies": [
                {
                    "author": r.get("author", {}).get("email", "unknown"),
                    "message": r.get("message", ""),
                    "updated": r.get("updated", ""),
                }
                for r in replies
            ],
        }

    threads = [_thread(c) for c in by_id.values() if not c.get("in_reply_to")]
    threads.sort(key=lambda t: (t["file"], t["line"] or 0))
    return threads


# ── Fetch ref ─────────────────────────────────────────────────────────────────


def _latest_patchset(api_base: str, change_id: str, project: str = "") -> str:
    """Return the latest patchset number for a change."""
    cid = f"{quote(project, safe='')}~{change_id}" if project else change_id
    data = _get(api_base, f"/changes/{cid}?o=CURRENT_REVISION")
    current = data.get("current_revision", "")
    revisions = data.get("revisions", {})
    if current and current in revisions:
        return str(revisions[current].get("_number", 1))
    # Fallback: max across all known revisions
    if revisions:
        return str(max(v.get("_number", 1) for v in revisions.values()))
    return "1"


def _git_remote_url(api_base: str, project: str) -> str:
    """Infer the git fetch URL from a Gerrit review host + project.

    chromium-review.googlesource.com + v8/v8
      → https://chromium.googlesource.com/v8/v8
    """
    host = urlparse(api_base).netloc
    git_host = re.sub(r"-review\.", ".", host)
    return f"https://{git_host}/{project}" if project else f"https://{git_host}"


def fetch_ref(
    change_url: str,
    repo_path: str = ".",
    fetch: bool = True,
) -> dict:
    """Return the git ref for a Gerrit CL patchset, optionally fetching it.

    Gerrit stores patchsets at refs/changes/NN/CHANGE_ID/PATCHSET where NN is
    the zero-padded last two digits of the change ID.

    If fetch=True, runs `git fetch <remote> <ref>` in repo_path so the ref is
    available locally as FETCH_HEAD.  The caller can then use standard git
    commands against FETCH_HEAD:

      git diff FETCH_HEAD          # diff vs working tree
      git diff main..FETCH_HEAD    # all changes in the CL vs main
      git log FETCH_HEAD           # CL commit history

    Returns:
      ref:         full git ref, e.g. refs/changes/74/7650974/2
      remote:      git remote URL, e.g. https://chromium.googlesource.com/v8/v8
      patchset:    patchset number used
      fetch_head:  commit SHA of FETCH_HEAD (only when fetch=True)
    """
    api_base, project, change_id, patchset = _parse_change_url(change_url)

    if not patchset:
        patchset = _latest_patchset(api_base, change_id, project)

    last_two = change_id[-2:].zfill(2)
    ref = f"refs/changes/{last_two}/{change_id}/{patchset}"
    remote = _git_remote_url(api_base, project)

    result: dict = {
        "ref": ref,
        "remote": remote,
        "patchset": patchset,
        "fetch_head": None,
    }

    if fetch:
        r = subprocess.run(
            ["git", "fetch", remote, ref],
            capture_output=True,
            text=True,
            cwd=repo_path,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git fetch failed: {r.stderr.strip()}")
        head = subprocess.run(
            ["git", "rev-parse", "FETCH_HEAD"],
            capture_output=True,
            text=True,
            cwd=repo_path,
        )
        result["fetch_head"] = head.stdout.strip()

    return result
