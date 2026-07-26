"""Shared helpers used by multiple MCP tool group modules."""

import os
import subprocess
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from .. import config
from .. import worktree as worktree_mod


_STARTUP_MTIME = os.path.getmtime(__file__)

# Overall byte cap for a single tool result, enforced by _text_result as a last
# line of defense. Individual tools already bound their output via pagination,
# match limits, etc; this only catches pathological cases those limits miss
# (e.g. a grep matching multi-hundred-KB minified lines or binary blobs). Set
# well below the 16 MiB stdio JSON-RPC message limit that, when exceeded, makes
# Claude Code tear down the whole MCP transport: truncating one result is far
# better than disconnecting the server. The text is measured in UTF-8 bytes;
# JSON escaping inflates this somewhat on the wire, hence the conservative gap.
_MAX_RESULT_BYTES = 8 * 1024 * 1024


def _truncate_to_bytes(text: str, limit: int) -> str:
    """Truncate text so its UTF-8 encoding is at most `limit` bytes.

    Returns text unchanged when already within the limit; otherwise keeps the
    head (most relevant) and appends a notice explaining the cap. Cuts on a
    character boundary so the result stays valid UTF-8.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    notice = (
        "\n\n[truncated: result exceeded the {mb} MiB tool-output cap "
        "({actual} bytes). Narrow the query (tighter path globs, lower limit, "
        "more specific pattern) to see the rest.]"
    ).format(mb=limit // (1024 * 1024), actual=len(encoded))
    budget = max(limit - len(notice.encode("utf-8")), 0)
    head = encoded[:budget].decode("utf-8", errors="ignore")
    return head + notice


def _check_stale() -> str:
    try:
        if os.path.getmtime(__file__) != _STARTUP_MTIME:
            return (
                "[WARNING: v8-utils was upgraded — "
                "restart the MCP server to use the new version]\n\n"
            )
    except OSError:
        pass
    return ""


def _text_result(text: str, *, stale_banner: bool = True) -> CallToolResult:
    """Return a CallToolResult with both content and structuredContent.

    Setting structuredContent.content makes Claude Code display the text
    with proper newlines instead of a collapsed JSON blob (see
    anthropics/claude-code#9962).

    stale_banner=False omits the "v8-utils was upgraded" prefix. Pass it for
    machine-readable payloads (format="json"): a programmatic consumer parses
    the body verbatim, so a prepended human banner turns the first char into a
    JSON syntax error rather than an advisory.
    """
    body = _truncate_to_bytes(text, _MAX_RESULT_BYTES)
    prefix = _check_stale() if stale_banner else ""
    return CallToolResult(
        content=[TextContent(type="text", text=prefix + body)],
    )


def _paginate(lines: list[str], offset: int, limit: int) -> tuple[list[str], int, int]:
    """Apply offset/limit pagination to a list of lines.

    offset: 0-based line offset. Negative values count from the end
            (e.g. -100 means last 100 lines).
    limit:  max lines to return.

    Returns (selected_lines, resolved_offset, total).
    """
    total = len(lines)
    if offset < 0:
        offset = max(total + offset, 0)
    selected = lines[offset : offset + limit]
    return selected, offset, total


def _paginate_result(
    lines: list[str], offset: int, limit: int, *, numbered: bool = False
) -> str:
    """Paginate lines and format with optional line numbers and truncation msg."""
    selected, offset, total = _paginate(lines, offset, limit)
    if numbered:
        result = "\n".join(
            f"{i + offset + 1:6}\t{line}" for i, line in enumerate(selected)
        )
    else:
        result = "\n".join(selected)
    if offset + limit < total:
        result += (
            f"\n(showing lines {offset + 1}–{offset + len(selected)}"
            f" of {total}; use offset/limit to paginate)"
        )
    return result


# Worktree selected for a repo via repo_git_worktree_select, keyed by repo name.
# Process-global, but each Claude Code session spawns its own server, so this is
# effectively session state. A session's subagents do share it -- that is what
# the per-call `worktree` parameter is for.
#
# Reach it only through the accessors below. Importing the dict itself binds the
# object at import time, so a caller that rebinds this name (a test isolating
# state, say) would leave readers and writers on two different dicts.
_active_worktree: dict[str, Path] = {}


def _selected_worktree(repo: str) -> Path | None:
    """Path selected for `repo`, or None when the main checkout is in use."""
    return _active_worktree.get(repo)


def _select_worktree(repo: str, path: Path) -> None:
    _active_worktree[repo] = path


def _clear_worktree(repo: str) -> Path | None:
    """Drop any selection for `repo`, returning what was selected."""
    return _active_worktree.pop(repo, None)


def _worktree_map(root: Path) -> dict[str, Path]:
    """Map every worktree of `root`'s repo to its path, by dir name and branch.

    git reports absolute paths for all worktrees from any member of the set, so
    no configuration beyond the repo entry point is needed. Directory names are
    added last: they win a collision with an unrelated worktree's branch name.
    """
    try:
        worktrees = worktree_mod.list_worktrees(root)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ValueError(f"Cannot list worktrees for {root}: {exc}") from exc
    by_branch: dict[str, Path] = {}
    by_dir: dict[str, Path] = {}
    for wt in worktrees:
        path = Path(wt["path"])
        branch = wt.get("branch")
        if branch and branch != "(detached)":
            by_branch[branch] = path
        by_dir[path.name] = path
    return {**by_branch, **by_dir}


def _resolve_worktree(repo: str, name: str) -> Path:
    """Resolve a worktree name (directory or branch) to its absolute path."""
    root = _configured_repo(repo)
    candidates = _worktree_map(root)
    path = candidates.get(name)
    if path is None:
        valid = ", ".join(sorted(candidates))
        raise ValueError(
            f"Unknown worktree {name!r} in repo {repo!r}. Available: {valid}"
        )
    if not path.is_dir():
        raise ValueError(f"Worktree {name!r} path does not exist: {path}")
    return path


def _configured_repo(repo: str) -> Path:
    """Resolve a repo name to its configured path, ignoring worktree selection."""
    cfg = config.load()
    entry = cfg.repos.get(repo)
    if entry is None:
        valid = ", ".join(sorted(cfg.repos))
        raise ValueError(f"Unknown repo {repo!r}. Configured repos: {valid}")
    if not entry.path.is_dir():
        raise ValueError(f"Repo {repo!r} path does not exist: {entry.path}")
    return entry.path


def _resolve_repo(repo: str, worktree: str | None = None) -> Path:
    """Resolve a repo name to the path git commands should run in.

    Precedence: the explicit `worktree` argument, then the worktree selected for
    this repo via repo_git_worktree_select, then the configured path.
    """
    if worktree is not None:
        return _resolve_worktree(repo, worktree)
    selected = _selected_worktree(repo)
    if selected is not None:
        if not selected.is_dir():
            # Removed while selected: say so rather than silently reading main.
            raise ValueError(
                f"Selected worktree {selected.name!r} no longer exists at {selected}. "
                f"Call repo_git_worktree_select with no name to return to the "
                f"main checkout."
            )
        return selected
    return _configured_repo(repo)


def _current_branch(root: Path) -> str:
    """Branch checked out at `root`, or "detached at <short hash>"."""
    r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    branch = r.stdout.strip()
    if r.returncode != 0 or not branch:
        return "?"
    if branch != "HEAD":
        return branch
    # Detached: --abbrev-ref reports the literal string "HEAD", which says
    # nothing about where the worktree actually is.
    r = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    short = r.stdout.strip()
    return f"detached at {short}" if r.returncode == 0 and short else "detached"


def _repo_banner(repo: str, root: Path) -> str:
    """Result prefix naming the worktree in use, empty for the main checkout.

    Worktree selection is sticky and otherwise invisible; this puts it in the
    transcript so drift is noticeable at the point it would mislead.
    """
    try:
        # Resolve both sides: the configured path is whatever the user wrote,
        # while worktree paths come from git already canonical, so a symlinked
        # or non-normalized config path would otherwise never compare equal.
        if root.resolve() == _configured_repo(repo).resolve():
            return ""
    except (ValueError, OSError):
        return ""
    return f"[{repo} @ {root.name} | branch {_current_branch(root)}]\n"
