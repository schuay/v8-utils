"""MCP tools for searching and reading companion source repos."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from ..api import repo_git
from ._shared import _text_result


REPOS_LINE = repo_git.repo_summary()
REPO_NAMES = repo_git.repo_names()

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
    for alias, entry in repo_git.configured_repos().items():
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
        return _text_result(
            repo_git.show(
                repo=repo,
                path=path,
                offset=offset,
                limit=limit,
                ref=ref,
                worktree=worktree,
            )
        )

    @mcp.tool(
        description=(
            "Search for a pattern in a related source repo using git grep.\n"
            "\n"
            "Returns bare file:line hits by default. Pass context=N to see the"
            " lines around each match; read the definition itself with"
            " repo_git_show."
        )
    )
    def repo_git_grep(
        repo: Annotated[str, Field(description=REPO_ARG)],
        pattern: Annotated[str, Field(description="regex pattern to search for")],
        glob: Annotated[
            str | None,
            Field(description='optional file glob filter, e.g. "*.cpp" or "*.{h,cpp}"'),
        ] = None,
        context: Annotated[
            int,
            Field(description="lines of context around each match"),
        ] = repo_git.DEFAULT_GREP_CONTEXT,
        ignore_case: Annotated[
            bool, Field(description="case-insensitive matching")
        ] = False,
        limit: Annotated[
            int,
            Field(
                description=("max matches to return; with context, max context blocks")
            ),
        ] = repo_git.MAX_GREP_MATCHES,
        ref: Annotated[str | None, Field(description=REF_ARG)] = None,
        worktree: Annotated[str | None, Field(description=WORKTREE_ARG)] = None,
    ) -> CallToolResult:
        return _text_result(
            repo_git.grep(
                repo=repo,
                pattern=pattern,
                glob=glob,
                context=context,
                ignore_case=ignore_case,
                limit=limit,
                ref=ref,
                worktree=worktree,
            )
        )

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
        limit: Annotated[
            int, Field(description="max files to return")
        ] = repo_git.MAX_LS_FILES,
        ref: Annotated[str | None, Field(description=REF_ARG)] = None,
        worktree: Annotated[str | None, Field(description=WORKTREE_ARG)] = None,
    ) -> CallToolResult:
        return _text_result(
            repo_git.find(repo=repo, glob=glob, limit=limit, ref=ref, worktree=worktree)
        )

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
            int,
            Field(description=f"max commits to return (max: {repo_git.MAX_LOG_LINES})"),
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
        return _text_result(
            repo_git.log(
                repo=repo,
                path=path,
                ref=ref,
                limit=limit,
                grep=grep,
                author=author,
                since=since,
                until=until,
                worktree=worktree,
            )
        )

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
            Field(
                description=f"window size in lines (max: {repo_git.MAX_BLAME_LINES})"
            ),
        ] = repo_git.DEFAULT_BLAME_LINES,
        ref: Annotated[str | None, Field(description=REF_ARG)] = None,
        worktree: Annotated[str | None, Field(description=WORKTREE_ARG)] = None,
    ) -> CallToolResult:
        return _text_result(
            repo_git.blame(
                repo=repo,
                path=path,
                start=start,
                limit=limit,
                ref=ref,
                worktree=worktree,
            )
        )

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
        return _text_result(repo_git.worktree_select(name=name, repo=repo))

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
        return _text_result(repo_git.worktree_list(repo=repo))
