"""Shared helpers used by multiple MCP tool group modules."""

import os
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from .. import config


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
