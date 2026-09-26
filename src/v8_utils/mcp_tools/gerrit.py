"""MCP tools for Chromium Gerrit code review."""

import re as _re
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from ..api import cq
from ..api import gerrit as gerrit_tools
from ..api import repo_git
from ._shared import _text_result

# Argument documentation lives on the argument (Annotated[..., Field(...)]) so a
# client sends it as the parameter's own schema description rather than leaving
# the model to match a prose line against a signature by name.
CHANGE_URL_ARG = (
    "Gerrit CL URL, e.g."
    " https://chromium-review.googlesource.com/c/v8/v8/+/7650974 or"
    " https://chromium-review.googlesource.com/7650974"
)


def _format_gerrit_comments(threads: list[dict]) -> str:
    blocks = []
    for t in threads:
        file = t["file"]
        if file == "/PATCHSET_LEVEL":
            loc = "(top-level)"
        else:
            loc = file
            if t.get("line"):
                loc += f":{t['line']}"
        if t.get("patch_set"):
            side = "Base" if t.get("side") == "PARENT" else f"ps{t['patch_set']}"
            commit = f" {t['commit_id'][:9]}" if t.get("commit_id") else ""
            loc += f" ({side}{commit})"
        tags = ""
        if t.get("draft"):
            tags += " [draft]"
        if t.get("unresolved"):
            tags += " [unresolved]"
        header = f"{loc}{tags}"
        author = t.get("author", "unknown")
        msg = t.get("message", "").strip()
        root_id = t.get("id")
        id_tag = f" [{root_id}]" if root_id else ""
        lines = [header, f"  {author}{id_tag}: {msg}"]
        for r in t.get("replies", []):
            r_author = r.get("author", "unknown")
            r_msg = r.get("message", "").strip()
            r_id = r.get("id")
            r_id_tag = f" [{r_id}]" if r_id else ""
            draft_tag = " [draft]" if r.get("draft") else ""
            lines.append(f"  {r_author}{r_id_tag}{draft_tag}: {r_msg}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _format_draft_results(results: list[dict]) -> str:
    def _loc(path: str | None, line: int | None) -> str:
        if path == "/PATCHSET_LEVEL" or not path:
            return "(top-level)"
        return f"{path}:{line}" if line else path

    lines = []
    for i, r in enumerate(results):
        if r.get("ok"):
            lines.append(
                f"[{i}] ok  {_loc(r.get('path'), r.get('line'))}  id={r.get('id', '?')}"
            )
        else:
            inp = r.get("input", {})
            lines.append(
                f"[{i}] FAIL {_loc(inp.get('path'), inp.get('line'))}  "
                f"{r.get('error', 'unknown error')}"
            )
    return "\n".join(lines)


def _format_cl_list(cls: list[dict]) -> str:
    """Format a list of compact change dicts into readable text."""
    blocks = []
    for cl in cls:
        # Label scores
        label_parts = []
        for label, votes in cl.get("labels", {}).items():
            scores = " ".join(f"{'+' if v > 0 else ''}{v}" for _, v in votes)
            # Shorten well-known labels
            short = label.replace("Code-Review", "CR").replace("Commit-Queue", "CQ")
            label_parts.append(f"{short}:{scores}")
        labels_str = f"  [{', '.join(label_parts)}]" if label_parts else ""

        wip = " (WIP)" if cl.get("wip") else ""
        comments = ""
        if cl.get("unresolved_comments"):
            comments = f"  {cl['unresolved_comments']} unresolved"

        line1 = f'{cl["number"]}  {cl["status"]}{wip}  "{cl["subject"]}"'
        line2 = (
            f"  {cl['owner']}  "
            f"+{cl['insertions']}/-{cl['deletions']}  "
            f"ps{cl.get('patchset', '?')}  "
            f"updated {cl['updated'][:10]}"
            f"{labels_str}{comments}"
        )

        lines = [line1, line2]

        if cl.get("reviewers"):
            lines.append(f"  reviewers: {', '.join(cl['reviewers'])}")

        if cl.get("attention"):
            attn = [f"{a['email']} ({a['reason']})" for a in cl["attention"]]
            lines.append(f"  attention: {', '.join(attn)}")

        blocks.append("\n".join(lines))

    header = f"{len(cls)} CL(s) found\n"
    return header + "\n\n".join(blocks)


def _comments_result(change_url: str, include_drafts: bool) -> CallToolResult:
    threads = gerrit_tools.comments(change_url, include_drafts=include_drafts)
    if not threads:
        return _text_result("No comments found.")
    return _text_result(_format_gerrit_comments(threads))


def register(
    mcp: FastMCP, *, drafts_enabled: bool = True, default_user: bool = True
) -> None:
    # When drafts are disabled (e.g. a shared/untrusted deployment) the
    # include_drafts parameter is removed entirely, so an agent cannot surface
    # the operator's unpublished review drafts.
    if drafts_enabled:

        @mcp.tool()
        def gerrit_comments(
            change_url: Annotated[str, Field(description=CHANGE_URL_ARG)],
            include_drafts: Annotated[
                bool,
                Field(
                    description=(
                        "also fetch your unpublished draft comments (requires"
                        " authentication via `luci-auth login`)"
                    )
                ),
            ] = False,
        ) -> CallToolResult:
            """Fetch comments on a Gerrit CL, threaded by file and line.

            Each entry represents a comment thread showing file:line, the short
            commit hash the comment is attached to, author, message, and replies.
            The commit hash identifies the exact code version — use `git show
            <hash>:path` to see the file as it was when the comment was written.

            Each comment line shows its UUID in `[brackets]` after the author.
            Pass that UUID as `in_reply_to` to `gerrit_create_comments` to reply.

            Threads are sorted by file path then line number.  Use this to understand
            reviewer feedback or the current state of a code review.

            """
            return _comments_result(change_url, include_drafts)

    else:

        @mcp.tool()
        def gerrit_comments(
            change_url: Annotated[str, Field(description=CHANGE_URL_ARG)],
        ) -> CallToolResult:
            """Fetch published comments on a Gerrit CL, threaded by file and line.

            Each entry represents a comment thread showing file:line, the short
            commit hash the comment is attached to, author, message, and replies.
            The commit hash identifies the exact code version — use `git show
            <hash>:path` to see the file as it was when the comment was written.

            Threads are sorted by file path then line number.  Use this to understand
            reviewer feedback or the current state of a code review.

            """
            return _comments_result(change_url, include_drafts=False)

    @mcp.tool()
    def gerrit_create_comments(
        change_url: Annotated[
            str,
            Field(
                description=(
                    f"{CHANGE_URL_ARG} A patchset suffix in the URL is honored"
                    " unless the `patchset` argument overrides it."
                )
            ),
        ],
        comments: Annotated[
            list[dict],
            Field(
                description=(
                    "per-comment dicts with these fields: message (required)"
                    " comment text; path (optional) file path, omit for a"
                    " top-level CL comment; line (optional) 1-based line number,"
                    " omit with no range for a file-level comment; side"
                    ' (optional) "REVISION" (default, the new patch) or "PARENT"'
                    " (the base it is diffed against); in_reply_to (optional)"
                    " UUID of an existing comment to reply to, from"
                    " `gerrit_comments` (shown as `[id]` in the output) -- a"
                    " reply with no path/line/range of its own is filed at its"
                    " parent's location and patchset, so a plain reply needs"
                    " only message + in_reply_to; unresolved (optional) bool,"
                    " default True; range (optional) {start_line,"
                    " start_character, end_line, end_character} for a multi-line"
                    " or character-range selection, which makes `line` ignored"
                )
            ),
        ],
        patchset: Annotated[
            int | str | None,
            Field(
                description=(
                    'revision id ("current", commit SHA, or patchset number).'
                    ' Default: the patchset from the URL, else "current".'
                )
            ),
        ] = None,
    ) -> CallToolResult:
        """Create one or more draft comments on a Gerrit CL revision.

        Drafts are private to you until published -- review them in the Gerrit UI
        or via `gerrit_comments` with `include_drafts=True`, then publish via
        Gerrit's "Reply" button.  Requires authentication via `luci-auth login`.

        Returns one result line per input, in order.  Each draft is created
        independently -- failures don't stop later ones.
        """
        results = gerrit_tools.create_drafts(change_url, comments, patchset=patchset)
        return _text_result(_format_draft_results(results))

    @mcp.tool()
    def gerrit_fetch(
        change_url: Annotated[
            str,
            Field(
                description=(
                    f"{CHANGE_URL_ARG} With or without a patchset suffix; if"
                    " none is given, the latest patchset is fetched."
                )
            ),
        ],
        v8_repo_path: Annotated[
            str | None,
            Field(
                description=(
                    "local v8 git repo to fetch into (default: the worktree"
                    " selected via repo_git_worktree_select, else the configured"
                    " v8 repo)"
                )
            ),
        ] = None,
        fetch: Annotated[
            bool,
            Field(
                description=(
                    "if False, return ref/remote without running git fetch --"
                    " useful for getting the ref name to fetch manually"
                )
            ),
        ] = True,
    ) -> dict:
        """Return the git ref for a Gerrit CL patchset, optionally fetching it.

        Gerrit stores each patchset at refs/changes/NN/CHANGE_ID/PATCHSET.
        If fetch=True (default), runs `git fetch` in v8_repo_path.

        Returns: ref, remote, patchset, fetch_head (commit SHA, if fetched)

        The patchset is fetched but NOT checked out — the working tree is
        unchanged.  To read file contents or diffs, use git commands that
        reference the commit directly.

        After a successful fetch, use the returned `fetch_head` SHA — do NOT
        use FETCH_HEAD (it may have changed by the time you run the next command):

          git show <fetch_head>                    # view the patchset commit
          git show <fetch_head>:path/to/file.cc   # read a file as it is in the patch
          git diff <fetch_head>^..<fetch_head>     # diff introduced by the commit
          git log <fetch_head>                     # history up to the patchset

        """
        repo_path = v8_repo_path or str(repo_git.resolve_repo("v8"))
        return gerrit_tools.fetch_ref(change_url, repo_path=repo_path, fetch=fetch)

    @mcp.tool()
    def gerrit_list_cls(
        query: Annotated[
            str,
            Field(
                description=(
                    'Gerrit search query, e.g. "owner:self status:open'
                    ' project:v8/v8", "reviewer:self -owner:self status:open'
                    ' project:v8/v8", "owner:self status:merged'
                    ' after:2026-03-01", "hashtag:compiler project:v8/v8'
                    ' status:open"'
                )
            ),
        ],
        limit: Annotated[int, Field(description="max results")] = 25,
    ) -> CallToolResult:
        """Search for Gerrit CLs on chromium-review.googlesource.com.

        Returns a compact summary of matching CLs: number, subject, status,
        owner, labels (Code-Review, Commit-Queue scores), reviewers, and
        attention set.

        "self" in queries is resolved to the configured user email.

        """
        if not default_user and _re.search(r"\bself\b", query):
            return _text_result(
                "Error: 'self' is disabled in this deployment; specify an "
                "explicit owner/reviewer email instead."
            )
        cls = gerrit_tools.list_cls(query, limit=limit)
        if not cls:
            return _text_result(f"No CLs found for query: {query}")
        return _text_result(_format_cl_list(cls))

    @mcp.tool()
    def gerrit_cq(
        change: Annotated[
            str,
            Field(
                description=('CL number or Gerrit URL, e.g. "7706944" or a full URL')
            ),
        ],
        patchset: Annotated[int, Field(description="patchset number")],
        builder: Annotated[
            str,
            Field(
                description=(
                    "builder name to zoom into (substring match, e.g."
                    ' "linux64_rel"). Omit for a pass/fail overview of every'
                    " bot."
                )
            ),
        ] = "",
        offset: Annotated[
            int, Field(description="line offset into builder detail output")
        ] = 0,
        limit: Annotated[
            int, Field(description="max lines to return for builder detail")
        ] = 200,
    ) -> CallToolResult:
        """Show CQ bot results for a Gerrit CL.

        Without builder: returns a compact overview of which bots passed/failed.
        With builder: zooms into that bot's failure logs (with backtraces).

        """
        return _text_result(
            cq.cq_report(
                change=change,
                patchset=patchset,
                builder=builder,
                offset=offset,
                limit=limit,
            )
        )
