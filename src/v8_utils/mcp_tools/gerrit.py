"""MCP tools for Chromium Gerrit code review."""

import re as _re
import shutil
import subprocess

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from .. import gerrit as gerrit_tools
from ..tools import _run_concurrent
from ._shared import _paginate_result, _resolve_repo, _text_result


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


# CQ / Buildbucket helpers


def _bb_run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a bb CLI command, raising ValueError on missing binary or auth."""
    bb = shutil.which("bb")
    if bb is None:
        raise ValueError(
            "bb (Buildbucket CLI) not found. "
            "Install depot_tools and ensure it is on PATH."
        )
    r = subprocess.run([bb, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        stderr = r.stderr.strip()
        if "Login required" in stderr or "not logged in" in stderr:
            raise ValueError(f"bb auth required: run 'bb auth-login'.\n{stderr}")
        if stderr:
            raise ValueError(f"bb {args[0]} failed: {stderr}")
    return r


def _parse_bb_jsonl(stdout: str) -> list[dict]:
    """Parse bb JSONL output (one JSON object per line)."""
    import json

    builds = []
    for line in stdout.strip().splitlines():
        line = line.strip()
        if line:
            try:
                builds.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return builds


def _bb_builder_name(build: dict) -> str:
    """Extract 'project/bucket/builder' from a build dict."""
    b = build.get("builder", {})
    return "/".join(
        p for p in [b.get("project"), b.get("bucket"), b.get("builder")] if p
    )


def _bb_categorize(builds: list[dict]) -> dict[str, list[dict]]:
    """Group builds by status category, deduplicating by builder name.

    When multiple CQ attempts produce builds for the same builder,
    keeps only the latest (highest build id) per builder name per category.
    """
    cats: dict[str, list[dict]] = {
        "SUCCESS": [],
        "FAILURE": [],
        "INFRA_FAILURE": [],
        "RUNNING": [],
        "CANCELED": [],
    }
    for b in builds:
        status = b.get("status", "")
        if status in ("STARTED", "SCHEDULED"):
            cats["RUNNING"].append(b)
        elif status in cats:
            cats[status].append(b)
    # Deduplicate: keep latest build per builder name in each category
    for key in cats:
        seen: dict[str, dict] = {}
        for b in cats[key]:
            name = _bb_builder_name(b)
            if name not in seen or str(b.get("id", "")) > str(seen[name].get("id", "")):
                seen[name] = b
        cats[key] = list(seen.values())
    return cats


def _bb_leaf_failures(build: dict) -> list[str]:
    """Return leaf failed step names from a build (which already has steps)."""
    failed = [s for s in build.get("steps", []) if s.get("status") == "FAILURE"]
    names = {s["name"] for s in failed}
    return [
        s["name"]
        for s in failed
        if not any(o != s["name"] and o.startswith(s["name"] + "|") for o in names)
    ]


def _bb_short_name(build: dict) -> str:
    """Extract just the builder name (without project/bucket prefix)."""
    return build.get("builder", {}).get("builder", _bb_builder_name(build))


def _format_cq_overview(
    cl_number: str,
    patchset: int,
    cats: dict[str, list[dict]],
) -> str:
    """Format CQ results as a compact overview (no logs)."""
    n_pass = len(cats["SUCCESS"])
    n_fail = len(cats["FAILURE"])
    n_infra = len(cats["INFRA_FAILURE"])
    n_run = len(cats["RUNNING"])
    n_cancel = len(cats["CANCELED"])
    total = n_pass + n_fail + n_infra + n_run + n_cancel

    parts = []
    if n_pass:
        parts.append(f"{n_pass} passed")
    if n_fail:
        parts.append(f"{n_fail} failed")
    if n_infra:
        parts.append(f"{n_infra} infra failures")
    extra = ""
    if n_run:
        extra += f"; {n_run} running"
    if n_cancel:
        extra += f"; {n_cancel} canceled"

    lines = [
        f"CQ results for {cl_number}/{patchset}",
        "",
        f"Summary: {', '.join(parts)} (of {total} builds{extra})",
    ]

    if cats["RUNNING"]:
        lines.append("")
        lines.append("RUNNING:")
        for b in cats["RUNNING"]:
            lines.append(f"  {_bb_short_name(b)}")

    if cats["INFRA_FAILURE"]:
        lines.append("")
        lines.append("INFRA_FAILURE:")
        for b in cats["INFRA_FAILURE"]:
            sm = b.get("summaryMarkdown", "")
            detail = f"  ({sm[:200]})" if sm else ""
            lines.append(f"  {_bb_short_name(b)}{detail}")

    if cats["FAILURE"]:
        lines.append("")
        lines.append("FAILED:")
        for b in cats["FAILURE"]:
            step_names = _bb_leaf_failures(b)
            if step_names:
                steps_str = ", ".join(step_names[:3])
                if len(step_names) > 3:
                    steps_str += f", +{len(step_names) - 3} more"
                lines.append(f"  {_bb_short_name(b)}  ({steps_str})")
            else:
                lines.append(f"  {_bb_short_name(b)}")

    if n_pass:
        lines.append("")
        lines.append(f"{n_pass} passed (not shown)")

    lines.append("")
    lines.append("Use builder=<name> to zoom into a specific bot's failure logs.")

    return "\n".join(lines)


def _dedup_lines(text: str) -> str:
    """Collapse consecutive duplicate lines, showing count."""
    lines = text.splitlines()
    if not lines:
        return text
    out: list[str] = []
    prev = lines[0]
    count = 1
    for line in lines[1:]:
        if line == prev:
            count += 1
        else:
            out.append(prev if count == 1 else f"{prev}  (x{count})")
            prev = line
            count = 1
    out.append(prev if count == 1 else f"{prev}  (x{count})")
    return "\n".join(out)


_RE_INFRA_LOG = _re.compile(
    r"^\[?[DIW]\d{4}-\d{2}-\d{2}T"
    r"|^I\d{4} "
    r"|^INFO:"
    r"|^swarming_bot_logs:"
    r"|^Use of LUCI "
    r"|^[0-9a-f]{16}: "
)


def _strip_infra(lines: list[str]) -> list[str]:
    """Remove infrastructure log lines everywhere, then trim blank edges."""
    lines = [l for l in lines if not _RE_INFRA_LOG.match(l)]
    # Trim leading/trailing blank lines
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _clean_log(text: str) -> str:
    """Light cleanup of a build log: dedup lines, strip PASS/infra noise."""
    lines = [l for l in text.splitlines() if not l.rstrip().endswith(": PASS")]
    lines = _strip_infra(lines)
    return _dedup_lines("\n".join(lines))


def _format_cq_builder_detail(
    build: dict,
) -> str:
    """Fetch and format failure logs for a single builder."""
    builder = _bb_short_name(build)
    build_id = str(build.get("id", ""))
    step_names = _bb_leaf_failures(build)

    lines = [f"Failure details for {builder} (build {build_id})", ""]

    if not step_names:
        lines.append("(no failed steps found)")
        return "\n".join(lines)

    def fetch_log(step_name: str) -> tuple[str, str | None]:
        try:
            lr = _bb_run(["log", build_id, step_name, "stdout"], timeout=30)
            return step_name, lr.stdout
        except (ValueError, subprocess.TimeoutExpired):
            return step_name, None

    fns = [lambda s=s: fetch_log(s) for s in step_names]
    results = _run_concurrent(fns)

    for step_name, raw_log in results:
        lines.append(f"── {step_name} ──")
        if raw_log is None:
            lines.append("(log fetch failed or timed out)")
        else:
            lines.append(_clean_log(raw_log))
        lines.append("")

    return "\n".join(lines)


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
            change_url: str, include_drafts: bool = False
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

            change_url:     Gerrit CL URL, e.g.:
              https://chromium-review.googlesource.com/c/v8/v8/+/7650974
              https://chromium-review.googlesource.com/7650974
            include_drafts: also fetch your unpublished draft comments (requires
              authentication via `luci-auth login`)
            """
            return _comments_result(change_url, include_drafts)

    else:

        @mcp.tool()
        def gerrit_comments(change_url: str) -> CallToolResult:
            """Fetch published comments on a Gerrit CL, threaded by file and line.

            Each entry represents a comment thread showing file:line, the short
            commit hash the comment is attached to, author, message, and replies.
            The commit hash identifies the exact code version — use `git show
            <hash>:path` to see the file as it was when the comment was written.

            Threads are sorted by file path then line number.  Use this to understand
            reviewer feedback or the current state of a code review.

            change_url:     Gerrit CL URL, e.g.:
              https://chromium-review.googlesource.com/c/v8/v8/+/7650974
              https://chromium-review.googlesource.com/7650974
            """
            return _comments_result(change_url, include_drafts=False)

    @mcp.tool()
    def gerrit_create_comments(
        change_url: str,
        comments: list[dict],
        patchset: int | str | None = None,
    ) -> CallToolResult:
        """Create one or more draft comments on a Gerrit CL revision.

        Drafts are private to you until published — review them in the Gerrit UI
        or via `gerrit_comments` with `include_drafts=True`, then publish via
        Gerrit's "Reply" button.  Requires authentication via `luci-auth login`.

        change_url: Gerrit CL URL (patchset suffix in URL is honored unless the
                    `patchset` argument overrides it)
        comments:   list of per-comment dicts with these fields:
          message     (required) comment text
          path        (optional) file path. Omit for a top-level CL comment.
          line        (optional) 1-based line number; omit + no range for a
                      file-level comment
          side        (optional) "REVISION" (default, the new patch) or
                      "PARENT" (the base it's diffed against)
          in_reply_to (optional) UUID of an existing comment to reply to.
                      Get UUIDs from `gerrit_comments` (shown as `[id]` in
                      the output)
          unresolved  (optional) bool, default True
          range       (optional) {start_line, start_character, end_line,
                      end_character} for multi-line / character-range selection.
                      When set, `line` is ignored.
        patchset:   revision id ("current", commit SHA, or patchset number).
                    Default: patchset from URL, else "current".

        Returns one result line per input, in order.  Each draft is created
        independently — failures don't stop later ones.
        """
        results = gerrit_tools.create_drafts(change_url, comments, patchset=patchset)
        return _text_result(_format_draft_results(results))

    @mcp.tool()
    def gerrit_fetch(
        change_url: str,
        v8_repo_path: str | None = None,
        fetch: bool = True,
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

        If no patchset is in the URL, the latest patchset is fetched.

        change_url:    Gerrit CL URL (with or without patchset suffix)
        v8_repo_path:  local v8 git repo to fetch into (default: configured v8 repo)
        fetch:         if False, return ref/remote without running git fetch
                       (useful for getting the ref name to fetch manually)
        """
        repo_path = v8_repo_path or str(_resolve_repo("v8"))
        return gerrit_tools.fetch_ref(change_url, repo_path=repo_path, fetch=fetch)

    @mcp.tool()
    def gerrit_list_cls(query: str, limit: int = 25) -> CallToolResult:
        """Search for Gerrit CLs on chromium-review.googlesource.com.

        Returns a compact summary of matching CLs: number, subject, status,
        owner, labels (Code-Review, Commit-Queue scores), reviewers, and
        attention set.

        "self" in queries is resolved to the configured user email.

        query: Gerrit search query, e.g.:
          "owner:self status:open project:v8/v8"
          "reviewer:self -owner:self status:open project:v8/v8"
          "owner:self status:merged after:2026-03-01"
          "hashtag:compiler project:v8/v8 status:open"
        limit: max results (default 25)
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
        change: str,
        patchset: int,
        builder: str = "",
        offset: int = 0,
        limit: int = 200,
    ) -> CallToolResult:
        """Show CQ bot results for a Gerrit CL.

        Without builder: returns a compact overview of which bots passed/failed.
        With builder: zooms into that bot's failure logs (with backtraces).

        change:    CL number or Gerrit URL (e.g. "7706944" or full URL)
        patchset:  patchset number
        builder:   builder name to zoom into (substring match, e.g. "linux64_rel")
        offset:    line offset into builder detail output (default 0)
        limit:     max lines to return for builder detail (default 200)
        """
        from ..pinpoint_cache import parse_patch_fields

        # Parse CL number from URL or bare number
        _, cl_number, _ = parse_patch_fields(change)
        if not cl_number:
            # Try bare number
            stripped = change.strip().split("/")[0]
            if stripped.isdigit():
                cl_number = stripped
            else:
                return _text_result(f"Error: cannot parse CL number from {change!r}")

        cl_spec = f"chromium-review.googlesource.com/c/v8/v8/+/{cl_number}/{patchset}"

        try:
            r = _bb_run(["ls", "-cl", cl_spec, "-json", "-steps"])
        except ValueError as e:
            return _text_result(f"Error: {e}")

        builds = _parse_bb_jsonl(r.stdout)
        if not builds:
            return _text_result(
                f"No builds found for CL {cl_number} patchset {patchset}."
            )

        cats = _bb_categorize(builds)

        if not builder:
            return _text_result(_format_cq_overview(cl_number, patchset, cats))

        # Zoom into a specific builder
        matches = [
            b for b in cats["FAILURE"] if builder.lower() in _bb_builder_name(b).lower()
        ]
        if not matches:
            all_failed = [_bb_short_name(b) for b in cats["FAILURE"]]
            return _text_result(
                f"No failed builder matching {builder!r}.\n"
                f"Failed builders: {', '.join(all_failed) or '(none)'}"
            )
        if len(matches) > 1:
            names = [_bb_short_name(b) for b in matches]
            return _text_result(
                f"Multiple builders match {builder!r}: {', '.join(names)}\n"
                f"Be more specific."
            )

        full = _format_cq_builder_detail(matches[0])
        return _text_result(_paginate_result(full.splitlines(), offset, limit))
