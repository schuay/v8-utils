"""Gerrit REST API tools."""

from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import quote, urlparse

import httpx


_XSSI = ")]}'\n"

# Gerrit hosts we are willing to talk to. The host is taken from the
# caller-supplied change URL, and authenticated requests attach the user's
# luci OAuth token; without this allowlist a crafted URL would send that token
# (and, via fetch_ref, a git fetch) to an arbitrary host. Only Google-operated
# Gerrit review hosts qualify (chromium-review, chrome-internal-review, ...).
_ALLOWED_HOST_SUFFIX = "-review.googlesource.com"


# ── URL parsing ───────────────────────────────────────────────────────────────


def _check_host(netloc: str) -> None:
    """Reject any host that is not a Google-operated Gerrit review host."""
    host = netloc.rsplit("@", 1)[-1].rsplit(":", 1)[0].lower()
    if not host.endswith(_ALLOWED_HOST_SUFFIX):
        raise ValueError(
            f"Refusing Gerrit request to non-allowlisted host {host!r}; "
            f"only *{_ALLOWED_HOST_SUFFIX} is permitted."
        )


def _parse_change_url(url: str) -> tuple[str, str, str, str | None]:
    """Parse a Gerrit change URL into (api_base, project, change_id, patchset).

    Accepts:
      https://chromium-review.googlesource.com/c/v8/v8/+/7650974
      https://chromium-review.googlesource.com/c/v8/v8/+/7650974/1
      https://chromium-review.googlesource.com/7650974
      https://chromium-review.googlesource.com/7650974/1
    """
    p = urlparse(url)
    if p.scheme != "https":
        raise ValueError(f"Gerrit URL must be https, got {p.scheme!r}: {url!r}")
    _check_host(p.netloc)
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
    """Get a Gerrit access token via git-credential-luci.

    Deliberately NOT cached, even though _get now asks for a token on every
    read.  git-credential-luci hands out short-lived OAuth access tokens
    (ya29.*, ~1h) and does its own caching and refresh, so a process-lifetime
    cache here would pin an expired token in any daemon that outlives it -- and
    the 401 fallback in _get would then quietly send every request anonymously,
    reintroducing the rate limiting this exists to avoid.  The helper costs
    ~15ms against a 30s HTTP timeout, so there is nothing to win.
    """
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


def _auth_error(status: int, detail: str = "") -> ValueError:
    """Build an actionable error for a Gerrit 401/403.

    These almost always mean the luci credentials are missing or expired, or
    the configured account lacks access, so the message spells out the fix
    rather than surfacing a bare HTTP status.
    """
    from . import config

    body = f" Gerrit said: {detail}\n" if detail else "\n"
    return ValueError(
        f"Gerrit returned HTTP {status} (permission denied).{body}"
        "Your luci credentials are likely missing or expired. To fix:\n"
        "  1. Authenticate:  git-credential-luci login\n"
        f"  2. Set your @chromium.org email in {config.CONFIG_PATH}:\n"
        '       user = "you@chromium.org"'
    )


def _parse_json(r: httpx.Response) -> dict | list:
    if r.status_code in (401, 403):
        raise _auth_error(r.status_code, r.text.strip()[:200])
    r.raise_for_status()
    text = r.text
    if text.startswith(_XSSI):
        text = text[len(_XSSI) :]
    return json.loads(text)


def _require_auth() -> str:
    token = _gerrit_token()
    if not token:
        raise ValueError(
            "Gerrit authentication required but git-credential-luci "
            "returned no token. Visit\n"
            "  https://chromium.googlesource.com/new-password\n"
            "to set up credentials."
        )
    return token


def _get(api_base: str, path: str, *, auth_required: bool = False) -> dict | list:
    """GET against the Gerrit REST API.

    Authenticated whenever a token is available, anonymous otherwise.  The
    order matters for RATE LIMITING, not for access: gerrit's anonymous quota
    is small and shared per IP, so it covers every tool on the box plus any
    human browsing at the same moment.  A public CL answers 200 anonymously, so
    trying anonymous first meant a poll never upgraded and drew from that shared
    bucket forever -- measured, 25 rapid anonymous reads of a public CL returned
    429 eleven times while 25 authenticated ones returned 200 every time.

    Falling back keeps a box with no luci credentials working against public
    CLs, which is why this is not simply auth_required=True everywhere.

    auth_required still forces the authenticated endpoint and raises when no
    token exists -- for /drafts and friends, where anonymous is not merely
    slower but wrong.
    """
    if auth_required:
        token = _require_auth()
    else:
        token = _gerrit_token()
    if token:
        r = httpx.get(
            f"{api_base}/a{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        # An expired or wrong-account token must not be worse than no token at
        # all: on a public endpoint, retry anonymously rather than raising the
        # "credentials missing or expired" error at a caller that never needed
        # them.
        if r.status_code in (401, 403) and not auth_required:
            r = httpx.get(f"{api_base}{path}", timeout=30)
    else:
        r = httpx.get(f"{api_base}{path}", timeout=30)
    return _parse_json(r)


def _put_json(api_base: str, path: str, body: dict) -> dict | list:
    """Authenticated PUT with a JSON body. Always uses /a/ prefix.

    On non-2xx responses, raises RuntimeError with the response body
    (Gerrit returns useful error text there, e.g. "Invalid inReplyTo, ...").
    """
    token = _require_auth()
    r = httpx.put(
        f"{api_base}/a{path}",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if r.status_code in (401, 403):
        raise _auth_error(r.status_code, r.text.strip()[:200])
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text.strip()}")
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


def _extract_label_flags(labels: dict) -> dict[str, dict[str, bool]]:
    """Extract {label: {approved, rejected}} -- gerrit's own verdict per label.

    Gerrit sets `approved`/`rejected` when someone cast the label's max/min
    value, which is what a caller asking "does this stand approved" wants: the
    numeric range is per-project and not discoverable from the votes alone
    (v8/v8's Code-Review is -1..+1, so a hardcoded ">= +2" never fires).

    The two are not complements, and `rejected` is not a veto signal: on a label
    carrying both a max and a min vote gerrit reports only `approved` (real
    case: v8/v8 8136620, -1 and +1, `approved` set, `rejected` absent, and
    `submittable` false). Decide a veto from the vote values in
    _extract_label_scores; use these flags only for "a max/min vote exists".
    """
    out: dict[str, dict[str, bool]] = {}
    for label_name, label_info in labels.items():
        approved, rejected = "approved" in label_info, "rejected" in label_info
        if approved or rejected:
            out[label_name] = {"approved": approved, "rejected": rejected}
    return out


def _compact_change(change: dict) -> dict:
    """Distill a ChangeInfo into a compact dict for display."""
    owner = change.get("owner", {})
    raw_labels = change.get("labels", {})
    labels = _extract_label_scores(raw_labels)

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
        "label_flags": _extract_label_flags(raw_labels),
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


def open_cls(query: str, limit: int = 50) -> list[dict]:
    """Open CLs matching `query`, each with its current patchset's revision and
    git-fetchable ref -- what a watcher needs to fetch and review a patchset, and
    which list_cls (LABELS/DETAILED_ACCOUNTS only) does not carry.

    query: a Gerrit search (e.g. "project:v8/v8 status:open owner:foo@google.com")
    Returns [{number, project, subject, owner, revision, fetch_ref}]: owner is the
    author email, revision the current patchset SHA, fetch_ref its refs/changes/...
    ref.
    """
    query = _resolve_self(query)
    params = (
        f"?q={quote(query, safe=':+')}&n={limit}&o=CURRENT_REVISION&o=DETAILED_ACCOUNTS"
    )
    changes: list = _get(_GERRIT_HOST, f"/changes/{params}")
    out: list[dict] = []
    for c in changes:
        rev = c.get("current_revision", "")
        revinfo = (c.get("revisions") or {}).get(rev, {})
        out.append(
            {
                "number": c.get("_number"),
                "project": c.get("project", ""),
                "subject": c.get("subject", ""),
                "owner": c.get("owner", {}).get("email", ""),
                "revision": rev,
                "fetch_ref": revinfo.get("ref", ""),
            }
        )
    return out


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
            "id": root.get("id"),
            "author": root.get("author", {}).get("email", "unknown"),
            "message": root.get("message", ""),
            "updated": root.get("updated", ""),
            "replies": [
                {
                    "id": r.get("id"),
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


# ── Drafts ────────────────────────────────────────────────────────────────────


def create_drafts(
    change_url: str,
    comments: list[dict],
    patchset: int | str | None = None,
) -> list[dict]:
    """Create one or more draft comments on a Gerrit CL revision.

    comments: list of per-comment dicts:
      message     (required) comment text
      path        (optional) file path. Omit for a top-level CL comment.
      line        (optional) 1-based line; omit + no range = file-level
      side        (optional) "REVISION" (default, after) or "PARENT" (before)
      in_reply_to (optional) UUID of comment to reply to
      unresolved  (optional) default True
      range       (optional) {start_line, start_character, end_line, end_character}

    patchset: revision identifier ("current", commit SHA, or patchset number).
              Defaults to the patchset in the URL, or "current".

    Returns one result per input, in order: on success a CommentInfo dict
    with {"ok": True}, on failure {"ok": False, "error": ..., "input": ...}.
    Continues past failures: each draft is persisted server-side independently.
    """
    api_base, project, change_id, url_patchset = _parse_change_url(change_url)
    cid = f"{quote(project, safe='')}~{change_id}" if project else change_id

    rev = patchset if patchset is not None else (url_patchset or "current")
    endpoint = f"/changes/{cid}/revisions/{rev}/drafts"

    results: list[dict] = []
    for c in comments:
        if not c.get("message"):
            results.append({"ok": False, "error": "missing field: message", "input": c})
            continue
        # Path is optional: omitted (or any /PATCHSET_LEVEL/ variant) means
        # a top-level CL comment. Gerrit's canonical magic path has no trailing
        # slash; trailing slash makes Gerrit treat it as a literal file path.
        path = c.get("path") or "/PATCHSET_LEVEL"
        if path.strip("/").upper() == "PATCHSET_LEVEL":
            path = "/PATCHSET_LEVEL"
        is_patchset_level = path == "/PATCHSET_LEVEL"
        body: dict = {
            "path": path,
            "message": c["message"],
            "unresolved": c.get("unresolved", True),
        }
        # Gerrit rejects side/line/range on patchset-level drafts.
        if not is_patchset_level:
            body["side"] = c.get("side", "REVISION")
            if c.get("line") is not None:
                body["line"] = c["line"]
            if c.get("range"):
                body["range"] = c["range"]
        if c.get("in_reply_to"):
            body["in_reply_to"] = c["in_reply_to"]
        try:
            info = _put_json(api_base, endpoint, body)
            if isinstance(info, dict):
                info["ok"] = True
                results.append(info)
            else:
                results.append(
                    {"ok": False, "error": f"unexpected response: {info!r}", "input": c}
                )
        except (httpx.HTTPError, ValueError, RuntimeError) as e:
            results.append({"ok": False, "error": str(e), "input": c})

    return results


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
