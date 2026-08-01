"""MCP tools for searching and reading companion source repos."""

import datetime
import subprocess
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from .. import config
from .. import worktree as worktree_mod
from ._shared import (
    _clear_worktree,
    _configured_repo,
    _current_branch,
    _paginate_result,
    _repo_banner,
    _resolve_repo,
    _resolve_worktree,
    _select_worktree,
    _selected_worktree,
    _text_result,
)


_MAX_READ_LINES = 2000
_MAX_GREP_MATCHES = 100
_MAX_LS_FILES = 500
_MAX_LOG_LINES = 2000
_MAX_BLAME_LINES = 1000
_DEFAULT_BLAME_LINES = 100


def _parse_blame_porcelain(text: str) -> tuple[list[tuple[str, int, str]], dict]:
    """Parse `git blame --porcelain` output.

    Returns (lines, commits) where lines is a list of
    (short_hash, final_line_number, content) and commits maps short_hash to
    {"date": "YYYY-MM-DD", "author": str, "summary": str}. Per-commit headers
    appear only the first time a commit is seen in porcelain output, so we
    cache them and reuse for subsequent lines.
    """
    lines: list[tuple[str, int, str]] = []
    commits: dict[str, dict] = {}
    full_details: dict[str, dict] = {}

    cur_hash = None
    cur_final = 0
    pending = {}
    for raw in text.splitlines():
        if raw.startswith("\t"):
            # Content line ends the current block.
            short = cur_hash[:9]
            if cur_hash not in full_details:
                full_details[cur_hash] = pending
                date = ""
                if "author-time" in pending:
                    tz = pending.get("author-tz", "+0000")
                    sign = 1 if tz[0] == "+" else -1
                    offset = datetime.timedelta(
                        hours=int(tz[1:3]), minutes=int(tz[3:5])
                    )
                    dt = datetime.datetime.fromtimestamp(
                        int(pending["author-time"]),
                        tz=datetime.timezone(sign * offset),
                    )
                    date = dt.strftime("%Y-%m-%d")
                commits[short] = {
                    "date": date,
                    "author": pending.get("author", ""),
                    "summary": pending.get("summary", ""),
                }
            lines.append((short, cur_final, raw[1:]))
            pending = {}
            continue

        parts = raw.split(" ", 1)
        if (
            len(parts) == 2
            and len(parts[0]) == 40
            and all(c in "0123456789abcdef" for c in parts[0])
        ):
            # Header line: "<40-hex> <orig> <final> [<count>]".
            hdr = parts[1].split(" ")
            cur_hash = parts[0]
            cur_final = int(hdr[1])
        elif len(parts) == 2:
            pending[parts[0]] = parts[1]

    return lines, commits


def _repo_summary() -> str:
    """One-line summary of configured repos for embedding in tool descriptions."""
    cfg = config.load()
    parts = []
    for alias, entry in cfg.repos.items():
        if entry.path.is_dir():
            parts.append(f"{alias} ({entry.desc})" if entry.desc else alias)
    return ", ".join(parts)


REPOS_LINE = _repo_summary()


def _repo_names() -> str:
    """Bare alias list for per-tool descriptions.

    The server instructions carry the full name-and-description list once. A
    tool's own description is read when the repo has already been chosen, so
    repeating the prose five more times buys nothing; the valid names do.
    """
    cfg = config.load()
    return ", ".join(a for a, e in cfg.repos.items() if e.path.is_dir())


REPO_NAMES = _repo_names()

# Argument documentation belongs on the argument. A client sends each of these
# as the parameter's own `description` in the JSON schema, so the model reads it
# attached to the field rather than having to match a prose line against a
# signature by name -- and a rename moves the text with it instead of silently
# leaving the prose describing an argument that no longer exists.
#
# Shared here because these three are repeated across the read tools; the rest
# are inline at their parameter.
REPO_ARG = f"repo alias to read. One of: {REPO_NAMES}"
REF_ARG = (
    "git ref (commit hash, branch, tag) to read at. If omitted, reads the working tree."
)
WORKTREE_ARG = (
    "git worktree to read instead of the main checkout (directory or branch"
    " name; repo_git_worktree_list). This call only. Prefer over `ref` for"
    " branch work: `ref` sees commits, `worktree` sees the working tree"
    " including uncommitted edits."
)


def _register_repo_resources(mcp: FastMCP) -> None:
    """Register MCP resources for configured repos."""
    cfg = config.load()
    for alias, entry in cfg.repos.items():
        if not entry.path.is_dir():
            continue
        desc = entry.desc or str(entry.path)

        def _make_resource(a: str, d: str, p: str):
            @mcp.resource(f"repo://{a}", name=a, description=d)
            def _repo_resource():
                return p

            return _repo_resource

        _make_resource(alias, desc, str(entry.path))


def register(mcp: FastMCP) -> None:
    _register_repo_resources(mcp)

    @mcp.tool(
        description=(
            "Read lines from a file in a related source repo, or show a commit.\n"
            "\n"
            "Two modes:\n"
            "  1. File mode (path provided): returns `limit` lines from `offset`.\n"
            "     Use repo_git_grep to find the right offset first.\n"
            "  2. Commit mode (path omitted, ref required): shows the commit message\n"
            "     and diff for the given ref (like `git show <ref>`)."
        )
    )
    def repo_git_show(
        repo: Annotated[str, Field(description=REPO_ARG)],
        path: Annotated[
            str | None,
            Field(
                description=(
                    "file path relative to the repo root; omit for commit mode"
                )
            ),
        ] = None,
        offset: Annotated[
            int, Field(description="0-based line offset to start reading from")
        ] = 0,
        limit: Annotated[int, Field(description="max lines to return")] = 100,
        ref: Annotated[
            str | None,
            Field(
                description=(
                    "git ref (commit hash, branch, tag). Required for commit mode."
                )
            ),
        ] = None,
        worktree: Annotated[str | None, Field(description=WORKTREE_ARG)] = None,
    ) -> CallToolResult:
        root = _resolve_repo(repo, worktree)
        banner = _repo_banner(repo, root)

        if path is None:
            # Commit mode: show commit message + diff
            if not ref:
                raise ValueError("ref is required when path is omitted (commit mode)")
            proc = subprocess.run(
                ["git", "show", "--stat", "--patch", "--end-of-options", ref],
                capture_output=True,
                text=True,
                cwd=root,
            )
            if proc.returncode != 0:
                raise ValueError(f"git show {ref} failed: {proc.stderr.strip()[:500]}")
            lines = proc.stdout.splitlines()
        elif ref:
            proc = subprocess.run(
                ["git", "show", "--end-of-options", f"{ref}:{path}"],
                capture_output=True,
                text=True,
                cwd=root,
            )
            if proc.returncode != 0:
                raise ValueError(
                    f"git show {ref}:{path} failed: {proc.stderr.strip()[:500]}"
                )
            lines = proc.stdout.splitlines()
        else:
            root = root.resolve()
            target = (root / path).resolve()
            # Prevent path traversal outside repo root. is_relative_to is a true
            # path-boundary check; a string prefix test would admit siblings
            # that merely share the root's leading characters (e.g. v8-secrets
            # for a v8 root).
            if not target.is_relative_to(root):
                raise ValueError(f"Path escapes repo root: {path}")
            if not target.is_file():
                raise ValueError(f"File not found: {path} (in {root})")
            lines = target.read_text(errors="replace").splitlines()
        return _text_result(
            banner + _paginate_result(lines, offset, limit, numbered=True)
        )

    @mcp.tool(
        description="Search for a pattern in a related source repo using git grep."
    )
    def repo_git_grep(
        repo: Annotated[str, Field(description=REPO_ARG)],
        pattern: Annotated[str, Field(description="regex pattern to search for")],
        glob: Annotated[
            str | None,
            Field(description='optional file glob filter, e.g. "*.cpp" or "*.{h,cpp}"'),
        ] = None,
        context: Annotated[
            int, Field(description="lines of context around each match")
        ] = 0,
        ignore_case: Annotated[
            bool, Field(description="case-insensitive matching")
        ] = False,
        limit: Annotated[
            int, Field(description="max matches to return")
        ] = _MAX_GREP_MATCHES,
        ref: Annotated[str | None, Field(description=REF_ARG)] = None,
        worktree: Annotated[str | None, Field(description=WORKTREE_ARG)] = None,
    ) -> CallToolResult:
        root = _resolve_repo(repo, worktree)
        banner = _repo_banner(repo, root)
        cmd = ["git", "grep", "-n", "--no-color", "-E"]
        if ignore_case:
            cmd.append("-i")
        if context > 0:
            cmd.append(f"-C{context}")
        # -e keeps a leading-dash pattern from being parsed as an option;
        # --end-of-options does the same for a caller-supplied ref.
        cmd.extend(["-e", pattern])
        if ref:
            cmd.extend(["--end-of-options", ref])
        if glob:
            cmd.extend(["--", glob])

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=root,
        )
        collected: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                collected.append(line.rstrip("\n"))
                if len(collected) >= limit + 1:
                    proc.kill()
                    break
        finally:
            proc.wait()

        if not collected and proc.returncode == 1:
            return _text_result(banner + "No matches found.")
        if not collected and proc.returncode not in (0, 1, -9):
            stderr = proc.stderr.read() if proc.stderr else ""
            raise ValueError(f"git grep failed: {stderr.strip()[:500]}")

        if len(collected) > limit:
            result = "\n".join(collected[:limit])
            result += f"\n(truncated — showing first {limit} matches)"
        else:
            result = "\n".join(collected)
        return _text_result(banner + result)

    @mcp.tool(
        description=(
            "List files in a related source repo matching a glob pattern"
            " (git ls-files)."
        )
    )
    def repo_git_find(
        repo: Annotated[str, Field(description=REPO_ARG)],
        glob: Annotated[
            str,
            Field(
                description=(
                    'file glob pattern, e.g. "*.cpp", "src/**/*.h", "runtime/RegExp*"'
                )
            ),
        ],
        limit: Annotated[int, Field(description="max files to return")] = _MAX_LS_FILES,
        ref: Annotated[str | None, Field(description=REF_ARG)] = None,
        worktree: Annotated[str | None, Field(description=WORKTREE_ARG)] = None,
    ) -> CallToolResult:
        root = _resolve_repo(repo, worktree)
        banner = _repo_banner(repo, root)
        if ref:
            cmd = [
                "git",
                "ls-tree",
                "-r",
                "--name-only",
                "--end-of-options",
                ref,
                "--",
                glob,
            ]
        else:
            cmd = ["git", "ls-files", "--", glob]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=root,
        )
        collected: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                collected.append(line.rstrip("\n"))
                if len(collected) >= limit + 1:
                    proc.kill()
                    break
        finally:
            proc.wait()

        if not collected:
            return _text_result(banner + "No files found.")

        if len(collected) > limit:
            result = "\n".join(collected[:limit])
            result += f"\n(truncated — showing first {limit} files)"
        else:
            result = "\n".join(collected)
        return _text_result(banner + result)

    @mcp.tool(description="Show git log in a related source repo.")
    def repo_git_log(
        repo: Annotated[str, Field(description=REPO_ARG)],
        path: Annotated[
            str | None, Field(description="optional file path to show history for")
        ] = None,
        ref: Annotated[
            str | None, Field(description="git ref to start from (default: HEAD)")
        ] = None,
        limit: Annotated[
            int, Field(description=f"max commits to return (max: {_MAX_LOG_LINES})")
        ] = 20,
        grep: Annotated[
            str | None,
            Field(description="optional pattern to filter commit messages"),
        ] = None,
        author: Annotated[
            str | None,
            Field(
                description=(
                    "optional author filter (git --author regex; matches name or"
                    ' email, e.g. "jgruber" or "@google.com")'
                )
            ),
        ] = None,
        since: Annotated[
            str | None,
            Field(
                description=(
                    "optional lower date bound (git --since; absolute like"
                    ' "2026-01-01" or relative like "2 weeks ago")'
                )
            ),
        ] = None,
        until: Annotated[
            str | None,
            Field(description="optional upper date bound (git --until; same formats)"),
        ] = None,
        worktree: Annotated[str | None, Field(description=WORKTREE_ARG)] = None,
    ) -> CallToolResult:
        root = _resolve_repo(repo, worktree)
        banner = _repo_banner(repo, root)
        limit = max(1, min(limit, _MAX_LOG_LINES))
        cmd = [
            "git",
            "log",
            # Fetch one extra so we can tell the caller output was capped.
            f"-{limit + 1}",
            "--format=%h %as %an  %s",
        ]
        if grep:
            cmd.extend(["--grep", grep, "-i"])
        if author:
            cmd.extend(["--author", author])
        if since:
            cmd.extend(["--since", since])
        if until:
            cmd.extend(["--until", until])
        if ref:
            cmd.extend(["--end-of-options", ref])
        if path:
            cmd.extend(["--", path])

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=root,
        )
        if proc.returncode != 0:
            raise ValueError(f"git log failed: {proc.stderr.strip()[:500]}")
        lines = proc.stdout.strip().split("\n") if proc.stdout.strip() else []
        if not lines:
            return _text_result(banner + "No commits found.")
        if len(lines) > limit:
            result = "\n".join(lines[:limit])
            result += f"\n(truncated — showing first {limit} commits)"
        else:
            result = "\n".join(lines)
        return _text_result(banner + result)

    @mcp.tool(
        description=(
            "Show git blame for a file in a related source repo: which commit\n"
            "last touched each line.\n"
            "\n"
            "Returns a bounded window of `limit` lines starting at `start`; the\n"
            "file is NOT blamed in full by default (a whole-file blame is large\n"
            "and slow). git only blames the requested window, so narrow reads\n"
            "are cheap. To blame a range [a, b] found via repo_git_grep, pass\n"
            "start=a and limit=b-a+1.\n"
            "\n"
            "Output is compact for agent use. Each line is\n"
            "  <line#> <hash> <content>\n"
            "and a `Commits:` legend at the end maps each unique <hash> to its\n"
            "date, author, and summary, so per-commit metadata is not repeated\n"
            "on every line. When more lines follow the window, a continuation\n"
            "hint gives the next `start`.\n"
            "\n"
        )
    )
    def repo_git_blame(
        repo: Annotated[str, Field(description=REPO_ARG)],
        path: Annotated[str, Field(description="file path relative to the repo root")],
        start: Annotated[int, Field(description="first line to blame, 1-based")] = 1,
        limit: Annotated[
            int,
            Field(description=f"window size in lines (max: {_MAX_BLAME_LINES})"),
        ] = _DEFAULT_BLAME_LINES,
        ref: Annotated[str | None, Field(description=REF_ARG)] = None,
        worktree: Annotated[str | None, Field(description=WORKTREE_ARG)] = None,
    ) -> CallToolResult:
        root = _resolve_repo(repo, worktree)
        banner = _repo_banner(repo, root)
        if start < 1:
            raise ValueError("start must be >= 1")
        limit = max(1, min(limit, _MAX_BLAME_LINES))

        # Blame only the requested window. Request one extra line so we can tell
        # the caller whether more lines follow without blaming the whole file.
        # git clamps the end of the range at EOF, so overshooting is harmless.
        cmd = ["git", "blame", "--porcelain", f"-L{start},+{limit + 1}"]
        if ref:
            # git blame does not honor a `--` path separator after
            # --end-of-options (unlike git log/grep), so pass the path as a
            # bare positional. --end-of-options still neutralizes a leading-dash
            # ref or path, keeping option injection impossible.
            cmd.extend(["--end-of-options", ref, path])
        else:
            cmd.extend(["--", path])

        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
        if proc.returncode != 0:
            raise ValueError(f"git blame failed: {proc.stderr.strip()[:500]}")

        lines, commits = _parse_blame_porcelain(proc.stdout)
        if not lines:
            return _text_result(
                banner + "No lines to blame (file empty or range out of range)."
            )

        more = len(lines) > limit
        shown = lines[:limit]
        width = max(len(str(n)) for _, n, _ in shown)
        body = "\n".join(f"{n:>{width}} {h} {content}" for h, n, content in shown)

        seen: list[str] = []
        for h, _, _ in shown:
            if h not in seen:
                seen.append(h)
        legend = "\n".join(
            f"  {h}  {commits[h]['date']}  {commits[h]['author']}"
            f"  {commits[h]['summary']}"
            for h in seen
        )

        out = banner + body + "\n\nCommits:\n" + legend
        if more:
            next_start = start + len(shown)
            out += (
                f"\n\n(showing lines {start}-{start + len(shown) - 1};"
                f" more follow, continue at start={next_start})"
            )
        return _text_result(out)

    @mcp.tool(
        description=(
            "Select the git worktree that repo_git_* tools read from.\n"
            "\n"
            "Call this first when asked to work in, investigate, or review a\n"
            "specific worktree or branch checkout: otherwise repo_git_* read the\n"
            "main checkout and silently return the wrong content for files the\n"
            "branch changed. Sticky for the session; results are then prefixed\n"
            "[repo @ name | branch ...]. Call with no name to return to main.\n"
            "\n"
            'Also redirects gerrit_fetch and Pinpoint exp_patch="auto" detection.\n'
            "Does NOT affect run_d8 or jsb_run_bench (pass their paths explicitly)."
        )
    )
    def repo_git_worktree_select(
        name: Annotated[
            str | None,
            Field(
                description=(
                    "worktree directory or branch name (repo_git_worktree_list)."
                    " Omit to return to the main checkout."
                )
            ),
        ] = None,
        repo: Annotated[str, Field(description="repo to select within")] = "v8",
    ) -> CallToolResult:
        if name is None:
            previous = _clear_worktree(repo)
            root = _configured_repo(repo)
            note = f" (was {previous.name})" if previous is not None else ""
            return _text_result(
                f"Using the main {repo} checkout{note}: {root}\n"
                f"branch {_current_branch(root)}"
            )

        path = _resolve_worktree(repo, name)
        _select_worktree(repo, path)

        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path,
            capture_output=True,
            text=True,
        )
        n_dirty = len([ln for ln in dirty.stdout.splitlines() if ln.strip()])
        clean = "clean" if n_dirty == 0 else f"{n_dirty} uncommitted file(s)"
        return _text_result(
            f"repo_git_* now read {repo} worktree {path.name}: {path}\n"
            f"branch {_current_branch(path)} | {clean}"
        )

    @mcp.tool(
        description=(
            "List the git worktrees of a configured repo, marking the selected\n"
            "one. These names are what repo_git_worktree_select and the\n"
            "`worktree` parameter accept."
        )
    )
    def repo_git_worktree_list(
        repo: Annotated[str, Field(description="repo to list worktrees for")] = "v8",
    ) -> CallToolResult:
        root = _configured_repo(repo)
        try:
            worktrees = worktree_mod.list_worktrees(root)
        except (subprocess.CalledProcessError, OSError) as exc:
            raise ValueError(f"Cannot list worktrees for {root}: {exc}") from exc
        if not worktrees:
            return _text_result("No worktrees found.")

        active = _selected_worktree(repo)
        lines = [f"{'':2} {'path':<50} {'branch':<32} head"]
        lines.append("-" * len(lines[0]))
        for wt in worktrees:
            mark = "*" if active is not None and Path(wt["path"]) == active else " "
            lines.append(
                f"{mark:2} {wt['path']:<50} {wt.get('branch', ''):<32}"
                f" {wt.get('head', '')}"
            )
        if active is None:
            lines.append("\nNo worktree selected; repo_git_* read the main checkout.")
        else:
            lines.append(f"\nSelected (*): repo_git_* read {active}")
        return _text_result("\n".join(lines))
