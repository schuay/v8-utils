"""Gerrit REST API tools."""

from __future__ import annotations

import json
import re
import shutil
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

# The Chromium redirector and where its /c/ links land. Only the public review
# host: crrev.com/i/ (chrome-internal-review) is a different host and is not
# claimed here until someone needs it.
_CRREV_HOST = "crrev.com"
_CRREV_TARGET = "chromium-review.googlesource.com"


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
      https://crrev.com/c/7650974
      https://crrev.com/c/7650974/1

    crrev.com is not a gerrit host, it is the redirector people paste: /c/N
    lands on chromium-review's short form, so it is rewritten to that here and
    never talked to. Rewritten BEFORE the host check, which would otherwise
    refuse it as a non-review host -- the one URL shape a chat thread cites
    most, refused as if it were hostile.
    """
    p = urlparse(url)
    if p.scheme != "https":
        raise ValueError(f"Gerrit URL must be https, got {p.scheme!r}: {url!r}")
    if p.netloc.lower() == _CRREV_HOST:
        m = re.match(r"^/c/(\d+)(?:/(\d+))?$", p.path.rstrip("/"))
        if not m:
            raise ValueError(f"Cannot parse crrev change URL: {url!r}")
        return f"https://{_CRREV_TARGET}", "", m.group(1), m.group(2)
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


def _no_token_reason() -> str:
    """Why _gerrit_token came back empty, as a line naming the actual fix.

    The two causes need opposite responses and are indistinguishable in the
    return value -- _gerrit_token catches FileNotFoundError (the helper is not on
    PATH) and CalledProcessError (it ran and holds no credentials) in one except,
    with stderr discarded, because its caller only asks "can I authenticate".

    Worth telling apart because the old single message named authentication, so
    a daemon whose PATH simply lacked depot_tools sent its operator to re-run
    `login` -- which succeeds, changes nothing, and gives no hint why. systemd
    user units are the common case: they do not source a shell profile, so a
    depot_tools directory that is on an interactive PATH is absent from theirs.
    """
    if shutil.which("git-credential-luci") is None:
        return (
            "git-credential-luci is not on PATH. It ships with depot_tools; add"
            " that checkout to PATH. In a systemd user unit set it explicitly --"
            " units do not source a shell profile, so a working interactive PATH"
            " says nothing about the daemon's."
        )
    return (
        "git-credential-luci is installed but holds no credentials for this"
        " account. Run `git-credential-luci login` as the user the process runs"
        " as -- a login under a different account does not carry over."
    )


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
        raise ValueError(f"Gerrit authentication required, but {_no_token_reason()}")
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


def _post_json(api_base: str, path: str, body: dict) -> dict | list:
    """Authenticated POST with a JSON body. Always uses /a/ prefix.

    Same contract as _put_json; separate because Gerrit splits create (PUT on
    /drafts) from act-on-the-change (POST on /review), and one helper taking a
    verb reads worse than two named for what they do.
    """
    token = _require_auth()
    r = httpx.post(
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

    `is_ai` is passed through, on the root and on every reply, because a caller
    that reads a thread it also WRITES to has no other way to tell its own
    replies from a reviewer's: an agent posting under a human's credentials
    shows up as that human, and re-reads its own comments as review to act on.
    Gerrit omits the field when unset, so absence means only "not marked" --
    it covers humans and anything written before the flag existed, and is never
    proof that a person wrote a comment.

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
                    **({"is_ai": True} if r.get("is_ai") else {}),
                }
                for r in replies
            ],
        }
        if root.get("is_ai"):
            t["is_ai"] = True
        if root.get("_draft"):
            t["draft"] = True
        return t

    threads = [_thread(c) for c in by_id.values() if not c.get("in_reply_to")]
    threads.sort(key=lambda t: (t["file"], t["line"] or 0))
    return threads


# ── Drafts ────────────────────────────────────────────────────────────────────


def _published_by_id(api_base: str, cid: str) -> dict[str, dict]:
    """Map comment id -> CommentInfo for every published comment on a change.

    The REST payload keys files at the top level and leaves `path` off the
    CommentInfo itself, so fold it in -- callers here want the location as one
    object.
    """
    data: dict = _get(api_base, f"/changes/{cid}/comments")
    out: dict[str, dict] = {}
    for filepath, cs in data.items():
        for c in cs:
            c["path"] = filepath
            out[c["id"]] = c
    return out


def _canonical_path(path: str | None) -> str:
    """The gerrit path a comment is filed under: a real file path, or the magic
    /PATCHSET_LEVEL for a change-level comment.

    Every /PATCHSET_LEVEL/ spelling normalizes to the canonical one, which has no
    trailing slash -- with one, gerrit takes it for a literal file named
    "PATCHSET_LEVEL" and the comment lands on a file nobody can see.
    """
    p = path or "/PATCHSET_LEVEL"
    return "/PATCHSET_LEVEL" if p.strip("/").upper() == "PATCHSET_LEVEL" else p


def _comment_input(c: dict, src: dict, path: str) -> dict:
    """A gerrit CommentInput built from one caller entry.

    `src` is where the LOCATION is read from: normally the entry itself, but a
    reply that inherits its parent's position passes the parent here. `path` is
    already canonical and is NOT included in the result -- create_drafts carries
    it inside the body while a review post keys its map by it, so each caller
    adds the path in the shape its endpoint wants.
    """
    body: dict = {
        "message": c["message"],
        "unresolved": c.get("unresolved", True),
    }
    # Gerrit rejects side/line/range on a patchset-level comment.
    if path != "/PATCHSET_LEVEL":
        body["side"] = src.get("side", "REVISION")
        if src.get("line") is not None:
            body["line"] = src["line"]
        if src.get("range"):
            body["range"] = src["range"]
    if c.get("in_reply_to"):
        body["in_reply_to"] = c["in_reply_to"]
    # Label machine-authored comments as such. Gerrit renders it, and a reviewer
    # is entitled to know a comment was not typed by a human -- depot_tools
    # stamps the same field on everything it writes under an AI agent.
    if c.get("is_ai"):
        body["is_ai"] = True
    return body


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
      is_ai       (optional) mark the draft as machine-authored
      unresolved  (optional) default True
      range       (optional) {start_line, start_character, end_line, end_character}

    A reply that names in_reply_to and pins no location of its own is filed at
    its parent's path, line, range, side and patchset. `in_reply_to` alone does
    not place a comment: gerrit stores the location exactly as sent, and its
    thread builder sorts by path before it walks in_reply_to, with
    /PATCHSET_LEVEL sorting ahead of every real file. A reply left at the
    default path is therefore visited before the parent it names, misses the
    lookup, and renders as its own detached thread.

    Location is inherited as a unit, so a caller that pins path (or line, or
    range) gets exactly what it asked for and no parent fields mixed in.

    patchset: revision identifier ("current", commit SHA, or patchset number).
              Defaults to the patchset in the URL, or "current". Applies to
              comments that are not location-inheriting replies; those follow
              their parent's patchset, which is where the thread lives.

    Returns one result per input, in order: on success a CommentInfo dict
    with {"ok": True}, on failure {"ok": False, "error": ..., "input": ...}.
    Continues past failures: each draft is persisted server-side independently.
    """
    api_base, project, change_id, url_patchset = _parse_change_url(change_url)
    cid = f"{quote(project, safe='')}~{change_id}" if project else change_id

    default_rev = patchset if patchset is not None else (url_patchset or "current")

    # Fetched at most once, and only if some reply actually needs a location.
    parents: dict[str, dict] = {}
    parents_error: str | None = None
    fetched = False

    def _parent(uuid: str) -> dict | None:
        nonlocal parents, parents_error, fetched
        if not fetched:
            fetched = True
            try:
                parents = _published_by_id(api_base, cid)
            except (httpx.HTTPError, ValueError, RuntimeError) as e:
                parents_error = str(e)
        return parents.get(uuid)

    results: list[dict] = []
    for c in comments:
        if not c.get("message"):
            results.append({"ok": False, "error": "missing field: message", "input": c})
            continue

        parent = None
        if c.get("in_reply_to") and not any(
            c.get(k) is not None for k in ("path", "line", "range")
        ):
            parent = _parent(c["in_reply_to"])
            if parent is None and parents_error:
                results.append(
                    {
                        "ok": False,
                        "error": f"could not read the parent comment: {parents_error}",
                        "input": c,
                    }
                )
                continue
        # An unknown uuid leaves parent None: fall through and let gerrit reject
        # it with "Invalid inReplyTo", which names the uuid.
        src = parent if parent is not None else c

        # Path is optional: omitted (or any /PATCHSET_LEVEL/ variant) means
        # a top-level CL comment. The draft endpoint carries the path in the
        # body, so it goes in alongside the shared CommentInput fields.
        path = _canonical_path(src.get("path"))
        body: dict = {"path": path, **_comment_input(c, src, path)}

        # An inherited reply goes on the patchset its parent was left on, so it
        # shows up in that diff view rather than only in the change-wide list.
        rev = default_rev
        if parent is not None and parent.get("patch_set") is not None:
            rev = parent["patch_set"]
        endpoint = f"/changes/{cid}/revisions/{rev}/drafts"
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


def post_review_comments(
    change_url: str,
    comments: list[dict],
    message: str = "",
    patchset: int | str | None = None,
) -> dict:
    """Publish inline comments on a CL in ONE request, without drafting first.

    comments: the same per-comment shape create_drafts takes (message, path,
    line, side, range, unresolved, is_ai), minus in_reply_to -- see below.

    The batched counterpart to create_drafts + publish_drafts, and a different
    tool rather than an optimization of them. Choose by whether a human reads the
    output before it lands:

      create_drafts (+ publish_drafts) -- private until someone publishes. The
        human IS the gate, and the drafts are what they review.
      post_review_comments -- published the moment it returns. Only for a caller
        whose output was already gated (a verify pass, or an explicit human
        request naming this CL).

    Three properties make this the right primitive for an automated publisher,
    and each is a failure mode of the draft-then-publish pair:

    - ATOMIC. One POST: the comments land together or not at all. The pair has a
      window where N drafts exist and the publish failed, leaving unpublished
      comments on someone's CL that nobody knows about and a retry duplicates.
    - EXACTLY SCOPED. Only the comments in this request are published.
      publish_drafts sends `PUBLISH_ALL_REVISIONS`, i.e. EVERY draft the account
      holds on the change -- including ones a human left by hand and was not
      finished with, and leftovers from an earlier failed run.
    - ONE ROUND TRIP, so a 50-comment review costs ~200ms rather than ~11s.

    `drafts: KEEP` is load-bearing, not a default worth trusting: gerrit's
    DraftHandling defaults to DELETE, so a review post that omits it DISCARDS the
    caller's unpublished drafts on that revision. depot_tools sends KEEP on every
    SetReview for the same reason. Silent data loss otherwise, and someone else's
    data at that.

    No labels, ever. Posting a review comment must not vote on the CL -- the same
    rule publish_drafts follows, and it matters more here: this path exists to be
    driven by an agent.

    Deliberately NOT exposed as an MCP tool, for the reason publish_drafts is
    not: create_drafts is safe to hand an agent because a draft is reversible and
    private, whereas this speaks as the operator on a colleague's CL the instant
    it is called. A caller wiring it to an agent owns the gate (authorization,
    scope, a cap) and holds it in its own code, where that gate can be reviewed.

    in_reply_to is NOT supported. A review post takes a map keyed by path, so a
    location-inheriting reply would need its parent fetched to be placed at all
    (the lookup create_drafts does). Replies are a conversation, which is the
    draft path's job; this one posts fresh review comments. Passing it is a hard
    error rather than a silently dropped field, which would file the reply as a
    detached top-level comment.

    patchset: revision identifier ("current", commit SHA, or patchset number).
              Defaults to the patchset in the URL, or "current". Pin it when the
              comments were written against a patchset that may have been
              superseded mid-review -- otherwise a newly uploaded one silently
              receives comments about code nobody read.

    `message` is the cover note posted alongside the comments.

    Returns gerrit's ReviewInfo. Raises on transport/auth failure or a rejected
    comment: unlike create_drafts there are no per-comment results to report,
    because nothing partially succeeds.
    """
    api_base, project, change_id, url_patchset = _parse_change_url(change_url)
    cid = f"{quote(project, safe='')}~{change_id}" if project else change_id
    rev = patchset if patchset is not None else (url_patchset or "current")

    # Validate everything before sending anything. The request is atomic, so a
    # bad entry must not reach gerrit and be rejected wholesale after a partial
    # body was built -- the caller gets one precise error naming the entry.
    by_path: dict[str, list[dict]] = {}
    for i, c in enumerate(comments):
        if not c.get("message"):
            raise ValueError(f"comment {i}: missing field: message")
        if c.get("in_reply_to"):
            raise ValueError(
                f"comment {i}: in_reply_to is not supported here; use create_drafts"
                " to reply to an existing comment"
            )
        path = _canonical_path(c.get("path"))
        by_path.setdefault(path, []).append(_comment_input(c, c, path))

    body: dict = {"drafts": "KEEP"}
    if by_path:
        body["comments"] = by_path
    if message:
        body["message"] = message
    if not by_path and not message:
        raise ValueError("nothing to post: no comments and no message")
    out = _post_json(api_base, f"/changes/{cid}/revisions/{rev}/review", body)
    return out if isinstance(out, dict) else {}


def publish_drafts(
    change_url: str,
    message: str = "",
    patchset: int | str | None = None,
) -> dict:
    """Publish the calling user's draft comments on a CL, so reviewers see them.

    Drafts created by create_drafts are private until this runs -- gerrit has no
    publish endpoint of its own, so it is a review post with
    `drafts: PUBLISH_ALL_REVISIONS`, which sends every draft the caller holds on
    the change rather than only those on one revision. That is the right scope
    here: a caller that drafted replies against the patchset a comment was left
    on, then uploaded a new one, would otherwise publish nothing.

    `message` is the cover note posted alongside; empty posts the drafts alone.
    No labels are ever set -- publishing a reply must not vote on the CL.

    Deliberately NOT exposed as an MCP tool. Creating a draft is reversible and
    private; publishing is neither, and an agent that can publish on the
    operator's behalf can speak as them on any CL they can reach.
    """
    api_base, project, change_id, url_patchset = _parse_change_url(change_url)
    cid = f"{quote(project, safe='')}~{change_id}" if project else change_id
    rev = patchset if patchset is not None else (url_patchset or "current")
    body: dict = {"drafts": "PUBLISH_ALL_REVISIONS"}
    if message:
        body["message"] = message
    out = _post_json(api_base, f"/changes/{cid}/revisions/{rev}/review", body)
    return out if isinstance(out, dict) else {}


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


def resolve_patchset(change_url: str) -> dict:
    """Pin a CL URL to one patchset and the SHA of its revision, without fetching.

    For a caller that has to know WHICH code a CL reference names before doing
    anything with it -- a chat approval binding to a patchset, an agent handed a
    citation it must be able to read -- and cannot pay for a fetch to find out.

    Not fetch_ref(fetch=False), which answers a different question. That returns
    the ref and the patchset NUMBER and no SHA (fetch_head is None without a
    fetch), and when the URL already names a patchset it makes no request at
    all: a mistyped or nonexistent change "resolves" cleanly and only fails
    later, wherever the ref is first used. This always asks gerrit, so a change
    that does not exist says so here.

    One query. `_latest_patchset` already makes it with o=CURRENT_REVISION and
    discards the SHA; ALL_REVISIONS carries the same for every patchset, which is
    what lets a URL naming an older one be pinned as exactly as the current one.

    Returns:
      ref:       full git ref, e.g. refs/changes/74/7650974/3
      patchset:  patchset number resolved (the URL's, else current)
      revision:  commit SHA of that patchset
      project:   gerrit project, e.g. v8/v8 -- authoritative even when the URL
                 omitted it, which is what lets a caller refuse a foreign one
      host:      the review host the change lives on
    """
    api_base, project, change_id, url_patchset = _parse_change_url(change_url)
    cid = f"{quote(project, safe='')}~{change_id}" if project else change_id
    data = _get(api_base, f"/changes/{cid}?o=ALL_REVISIONS")
    if not isinstance(data, dict) or not data.get("revisions"):
        raise ValueError(f"no such change, or it has no revisions: {change_url!r}")

    revisions = data["revisions"]
    if url_patchset:
        wanted = int(url_patchset)
        found = [
            (sha, rev) for sha, rev in revisions.items() if rev.get("_number") == wanted
        ]
        if not found:
            raise ValueError(f"change {change_id} has no patchset {wanted}")
        revision, rev = found[0]
    else:
        revision = data.get("current_revision", "")
        if revision not in revisions:
            raise ValueError(f"change {change_id} names no current revision")
        rev = revisions[revision]

    patchset = str(rev.get("_number", url_patchset or 1))
    last_two = change_id[-2:].zfill(2)
    return {
        "ref": f"refs/changes/{last_two}/{change_id}/{patchset}",
        "patchset": patchset,
        "revision": revision,
        # From the response, not the URL: the short form carries no project, and
        # a caller restricting citations to its own repo needs the real one.
        "project": data.get("project", project),
        "host": urlparse(api_base).netloc,
    }


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
