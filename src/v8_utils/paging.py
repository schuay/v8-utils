"""Offset/limit pagination of text lines."""


def paginate(lines: list[str], offset: int, limit: int) -> tuple[list[str], int, int]:
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


def paginate_result(
    lines: list[str], offset: int, limit: int, *, numbered: bool = False
) -> str:
    """Paginate lines and format with optional line numbers and truncation msg."""
    selected, offset, total = paginate(lines, offset, limit)
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
