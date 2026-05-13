"""MCP tools for searching and reading companion source repos."""

import subprocess

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from .. import config
from ._shared import _paginate_result, _resolve_repo, _text_result


_MAX_READ_LINES = 2000
_MAX_GREP_MATCHES = 100
_MAX_LS_FILES = 500
_MAX_LOG_LINES = 2000


def _repo_summary() -> str:
    """One-line summary of configured repos for embedding in tool descriptions."""
    cfg = config.load()
    parts = []
    for alias, entry in cfg.repos.items():
        if entry.path.is_dir():
            parts.append(f"{alias} ({entry.desc})" if entry.desc else alias)
    return ", ".join(parts)


REPOS_LINE = _repo_summary()


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
            "     and diff for the given ref (like `git show <ref>`).\n"
            "\n"
            f"Configured repos: {REPOS_LINE}\n"
            "\n"
            "repo:   repo name (see list above)\n"
            "path:   file path relative to the repo root (omit for commit mode)\n"
            "offset: 0-based line offset to start reading from (default: 0)\n"
            "limit:  max lines to return (default: 100)\n"
            "ref:    git ref (commit hash, branch, tag). Required for commit mode."
        )
    )
    def repo_git_show(
        repo: str,
        path: str | None = None,
        offset: int = 0,
        limit: int = 100,
        ref: str | None = None,
    ) -> CallToolResult:
        root = _resolve_repo(repo)

        if path is None:
            # Commit mode: show commit message + diff
            if not ref:
                raise ValueError("ref is required when path is omitted (commit mode)")
            proc = subprocess.run(
                ["git", "show", "--stat", "--patch", ref],
                capture_output=True,
                text=True,
                cwd=root,
            )
            if proc.returncode != 0:
                raise ValueError(f"git show {ref} failed: {proc.stderr.strip()[:500]}")
            lines = proc.stdout.splitlines()
        elif ref:
            proc = subprocess.run(
                ["git", "show", f"{ref}:{path}"],
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
            target = (root / path).resolve()
            # Prevent path traversal outside repo root
            if not str(target).startswith(str(root)):
                raise ValueError(f"Path escapes repo root: {path}")
            if not target.is_file():
                raise ValueError(f"File not found: {path} (in {root})")
            lines = target.read_text(errors="replace").splitlines()
        return _text_result(_paginate_result(lines, offset, limit, numbered=True))

    @mcp.tool(
        description=(
            "Search for a pattern in a related source repo using git grep.\n"
            "\n"
            f"Configured repos: {REPOS_LINE}\n"
            "\n"
            "repo:    repo name (see list above)\n"
            "pattern: regex pattern to search for\n"
            'glob:    optional file glob filter, e.g. "*.cpp" or "*.{h,cpp}"\n'
            "context: lines of context around each match (default: 0)\n"
            "ignore_case: case-insensitive matching (default: false)\n"
            "limit:   max matches to return (default: 100)\n"
            "ref:     git ref to search in (e.g. commit hash, branch, tag).\n"
            "         If omitted, searches the working tree."
        )
    )
    def repo_git_grep(
        repo: str,
        pattern: str,
        glob: str | None = None,
        context: int = 0,
        ignore_case: bool = False,
        limit: int = _MAX_GREP_MATCHES,
        ref: str | None = None,
    ) -> CallToolResult:
        root = _resolve_repo(repo)
        cmd = ["git", "grep", "-n", "--no-color", "-E"]
        if ignore_case:
            cmd.append("-i")
        if context > 0:
            cmd.append(f"-C{context}")
        cmd.append(pattern)
        if ref:
            cmd.append(ref)
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
            return _text_result("No matches found.")
        if not collected and proc.returncode not in (0, 1, -9):
            stderr = proc.stderr.read() if proc.stderr else ""
            raise ValueError(f"git grep failed: {stderr.strip()[:500]}")

        if len(collected) > limit:
            result = "\n".join(collected[:limit])
            result += f"\n(truncated — showing first {limit} matches)"
        else:
            result = "\n".join(collected)
        return _text_result(result)

    @mcp.tool(
        description=(
            "List files in a related source repo matching a glob pattern (git ls-files).\n"
            "\n"
            f"Configured repos: {REPOS_LINE}\n"
            "\n"
            "repo:   repo name (see list above)\n"
            'glob:   file glob pattern, e.g. "*.cpp", "src/**/*.h", "runtime/RegExp*"\n'
            "limit:  max files to return (default: 500)\n"
            "ref:    git ref to list from (e.g. commit hash, branch, tag).\n"
            "        If omitted, lists from the working tree."
        )
    )
    def repo_git_find(
        repo: str,
        glob: str,
        limit: int = _MAX_LS_FILES,
        ref: str | None = None,
    ) -> CallToolResult:
        root = _resolve_repo(repo)
        if ref:
            cmd = ["git", "ls-tree", "-r", "--name-only", ref, "--", glob]
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
            return _text_result("No files found.")

        if len(collected) > limit:
            result = "\n".join(collected[:limit])
            result += f"\n(truncated — showing first {limit} files)"
        else:
            result = "\n".join(collected)
        return _text_result(result)

    @mcp.tool(
        description=(
            "Show git log in a related source repo.\n"
            "\n"
            f"Configured repos: {REPOS_LINE}\n"
            "\n"
            "repo:   repo name (see list above)\n"
            "path:   optional file path to show history for\n"
            "ref:    git ref to start from (default: HEAD)\n"
            "limit:  max commits to return (default: 20)\n"
            "grep:   optional pattern to filter commit messages"
        )
    )
    def repo_git_log(
        repo: str,
        path: str | None = None,
        ref: str | None = None,
        limit: int = 20,
        grep: str | None = None,
    ) -> CallToolResult:
        root = _resolve_repo(repo)
        cmd = [
            "git",
            "log",
            f"-{limit}",
            "--format=%h %as %an  %s",
        ]
        if grep:
            cmd.extend(["--grep", grep, "-i"])
        if ref:
            cmd.append(ref)
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
        result = proc.stdout.strip()
        if not result:
            return _text_result("No commits found.")
        return _text_result(result)
