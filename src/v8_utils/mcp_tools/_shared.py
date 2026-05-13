"""Shared helpers used by multiple MCP tool group modules."""

import os
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from .. import config


_STARTUP_MTIME = os.path.getmtime(__file__)


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


def _text_result(text: str) -> CallToolResult:
    """Return a CallToolResult with both content and structuredContent.

    Setting structuredContent.content makes Claude Code display the text
    with proper newlines instead of a collapsed JSON blob (see
    anthropics/claude-code#9962).
    """
    return CallToolResult(
        content=[TextContent(type="text", text=_check_stale() + text)],
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


def _resolve_repo(repo: str) -> Path:
    """Resolve a repo name to its configured path, or raise ValueError."""
    cfg = config.load()
    entry = cfg.repos.get(repo)
    if entry is None:
        valid = ", ".join(sorted(cfg.repos))
        raise ValueError(f"Unknown repo {repo!r}. Configured repos: {valid}")
    if not entry.path.is_dir():
        raise ValueError(f"Repo {repo!r} path does not exist: {entry.path}")
    return entry.path
