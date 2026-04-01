"""Gerrit REST API tools."""

from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import quote, urlparse

import httpx


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


def _gerrit_token() -> str | None:
    """Get a Gerrit access token via git-credential-luci."""
    try:
        out = subprocess.check_output(
            ["git-credential-luci", "get"],
            input="",
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for line in out.splitlines():
            if line.startswith("password="):
                return line[len("password=") :]
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return None


def _parse_json(r: httpx.Response) -> dict | list:
    r.raise_for_status()
    text = r.text
    if text.startswith(_XSSI):
        text = text[len(_XSSI) :]
    return json.loads(text)


def _get(api_base: str, path: str, *, auth_required: bool = False) -> dict | list:
    """GET against the Gerrit REST API.

    Public endpoints are tried without auth first; on 401 we upgrade
    automatically.  When auth_required is True, we go straight to the
    authenticated endpoint and raise ValueError if no token is available.
    """
    if auth_required:
        token = _gerrit_token()
        if not token:
            raise ValueError(
                "Gerrit authentication required but git-credential-luci "
                "returned no token. Visit\n"
                "  https://chromium.googlesource.com/new-password\n"
                "to set up credentials."
            )
        return _parse_json(
            httpx.get(
                f"{api_base}/a{path}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
        )
    r = httpx.get(f"{api_base}{path}", timeout=30)
    if r.status_code == 401:
        token = _gerrit_token()
        if token:
            r = httpx.get(
                f"{api_base}/a{path}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
    return _parse_json(r)


# ── Query CLs ─────────────────────────────────────────────────────────────────

_GERRIT_HOST = "https://chromium-review.googlesource.com"

# Labels we care about for compact display.
_INTERESTING_LABELS = ("Code-Review", "Commit-Queue")


def _extract_label_scores(labels: dict) -> dict[str, list[tuple[str, int]]]:
    """Extract {label: [(email, value), ...]} from the labels dict."""
    result: dict[str, list[tuple[str, int]]] = {}
    for label_name, label_info in labels.items():
        votes = []
        for entry in label_info.get("all", []):
            value = entry.get("value", 0)
            if value != 0:
                email = entry.get("email", "unknown")
                votes.append((email, value))
        if votes:
            result[label_name] = votes
    return result


def _compact_change(change: dict) -> dict:
    """Distill a ChangeInfo into a compact dict for display."""
    owner = change.get("owner", {})
    labels = _extract_label_scores(change.get("labels", {}))

    # Attention set: extract account emails and reasons
    attention = []
    for _acct_id, info in change.get("attention_set", {}).items():
        acct = info.get("account", {})
        attention.append(
            {
                "email": acct.get("email", f"account/{acct.get('_account_id', '?')}"),
                "reason": info.get("reason", ""),
            }
        )

    # Reviewers (just emails, skip service accounts)
    reviewers = [
        r.get("email", "unknown")
        for r in change.get("reviewers", {}).get("REVIEWER", [])
        if "SERVICE_USER" not in r.get("tags", [])
    ]

    return {
        "number": change["_number"],
        "subject": change.get("subject", ""),
        "status": change.get("status", ""),
        "owner": owner.get("email", f"account/{owner.get('_account_id', '?')}"),
        "project": change.get("project", ""),
        "branch": change.get("branch", ""),
        "insertions": change.get("insertions", 0),
        "deletions": change.get("deletions", 0),
        "updated": change.get("updated", ""),
        "wip": change.get("work_in_progress", False),
        "hashtags": change.get("hashtags", []),
        "unresolved_comments": change.get("unresolved_comment_count", 0),
        "patchset": change.get("current_revision_number"),
        "labels": labels,
        "reviewers": reviewers,
        "attention": attention,
    }


def _resolve_self(query: str) -> str:
    """Replace 'self' in query operators with the configured user email."""
    from . import config

    cfg = config.load()
    if not cfg.user:
        return query
    # Replace owner:self, reviewer:self, etc. with the actual email
    return re.sub(r"\bself\b", cfg.user, query)


def list_cls(query: str, limit: int = 25) -> list[dict]:
    """Query Gerrit CLs and return compact change info.

    query: Gerrit search query (e.g. "owner:self status:open project:v8/v8")
    limit: max results (default 25)
    """
    query = _resolve_self(query)
    params = f"?q={quote(query, safe=':+')}&n={limit}&o=LABELS&o=DETAILED_ACCOUNTS"
    changes: list = _get(_GERRIT_HOST, f"/changes/{params}")
    return [_compact_change(c) for c in changes]


# ── Comments ──────────────────────────────────────────────────────────────────


def comments(change_url: str, *, include_drafts: bool = False) -> list[dict]:
    """Return all published comments on a CL, as a flat list of threads.

    Each thread has: file, line, patch_set, author, message, replies[].
    Threads are sorted by file then line.

    If include_drafts is True, also fetches your unpublished draft comments
    (requires authentication via `luci-auth login`).  Drafts are marked
    with draft=True.
    """
    api_base, project, change_id, _ = _parse_change_url(change_url)
    cid = f"{quote(project, safe='')}~{change_id}" if project else change_id
    data: dict = _get(api_base, f"/changes/{cid}/comments")

    # Build id → comment map
    by_id: dict[str, dict] = {}
    for filepath, cs in data.items():
        for c in cs:
            c["_file"] = filepath
            c["_draft"] = False
            by_id[c["id"]] = c

    if include_drafts:
        drafts: dict = _get(api_base, f"/changes/{cid}/drafts", auth_required=True)
        for filepath, ds in drafts.items():
            for d in ds:
                d["_file"] = filepath
                d["_draft"] = True
                d.setdefault("author", {"email": "me"})
                by_id[d["id"]] = d

    # Map each comment to its thread root by walking in_reply_to chains.
    def _find_root(c: dict) -> str:
        seen: set[str] = set()
        cur = c
        while cur.get("in_reply_to") and cur["in_reply_to"] in by_id:
            if cur["id"] in seen:
                break  # cycle guard
            seen.add(cur["id"])
            cur = by_id[cur["in_reply_to"]]
        return cur["id"]

    # Group all non-root comments by their thread root.
    children: dict[str, list[dict]] = {}
    for c in by_id.values():
        if c.get("in_reply_to"):
            root_id = _find_root(c)
            children.setdefault(root_id, []).append(c)

    # Root comments only; build thread for each
    def _thread(root: dict) -> dict:
        replies = sorted(
            children.get(root["id"], []),
            key=lambda c: c.get("updated", ""),
        )
        t = {
            "file": root["_file"],
            "line": root.get("line"),
            "patch_set": root.get("patch_set"),
            "side": root.get("side"),
            "commit_id": root.get("commit_id"),
            "unresolved": (replies[-1] if replies else root).get("unresolved", False),
            "author": root.get("author", {}).get("email", "unknown"),
            "message": root.get("message", ""),
            "updated": root.get("updated", ""),
            "replies": [
                {
                    "author": r.get("author", {}).get("email", "unknown"),
                    "message": r.get("message", ""),
                    "updated": r.get("updated", ""),
                    **({"draft": True} if r.get("_draft") else {}),
                }
                for r in replies
            ],
        }
        if root.get("_draft"):
            t["draft"] = True
        return t

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
