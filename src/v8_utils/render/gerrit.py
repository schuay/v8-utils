"""Text for Gerrit comment threads, CL listings and draft results, as the
MCP tools print them."""

from ..api import gerrit


def format_comments(threads: list[gerrit.CommentThread]) -> str:
    blocks = []
    for t in threads:
        if t.file == "/PATCHSET_LEVEL":
            loc = "(top-level)"
        else:
            loc = t.file
            if t.line:
                loc += f":{t.line}"
        if t.patch_set:
            side = "Base" if t.side == "PARENT" else f"ps{t.patch_set}"
            commit = f" {t.commit_id[:9]}" if t.commit_id else ""
            loc += f" ({side}{commit})"
        tags = ""
        if t.draft:
            tags += " [draft]"
        if t.unresolved:
            tags += " [unresolved]"
        header = f"{loc}{tags}"
        id_tag = f" [{t.id}]" if t.id else ""
        lines = [header, f"  {t.author}{id_tag}: {t.message.strip()}"]
        for r in t.replies:
            r_id_tag = f" [{r.id}]" if r.id else ""
            draft_tag = " [draft]" if r.draft else ""
            lines.append(f"  {r.author}{r_id_tag}{draft_tag}: {r.message.strip()}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_draft_results(results: list[gerrit.DraftResult]) -> str:
    def _loc(path: str | None, line: int | None) -> str:
        if path == "/PATCHSET_LEVEL" or not path:
            return "(top-level)"
        return f"{path}:{line}" if line else path

    lines = []
    for i, r in enumerate(results):
        if r.ok:
            lines.append(f"[{i}] ok  {_loc(r.path, r.line)}  id={r.id or '?'}")
        else:
            inp = r.input or {}
            lines.append(
                f"[{i}] FAIL {_loc(inp.get('path'), inp.get('line'))}  "
                f"{r.error or 'unknown error'}"
            )
    return "\n".join(lines)


def format_cl_list(cls: list[gerrit.Change]) -> str:
    """Format a list of compact changes into readable text."""
    blocks = []
    for cl in cls:
        # Label scores
        label_parts = []
        for label, votes in cl.labels.items():
            scores = " ".join(f"{'+' if v.value > 0 else ''}{v.value}" for v in votes)
            # Shorten well-known labels
            short = label.replace("Code-Review", "CR").replace("Commit-Queue", "CQ")
            label_parts.append(f"{short}:{scores}")
        labels_str = f"  [{', '.join(label_parts)}]" if label_parts else ""

        wip = " (WIP)" if cl.wip else ""
        comments = ""
        if cl.unresolved_comments:
            comments = f"  {cl.unresolved_comments} unresolved"

        line1 = f'{cl.number}  {cl.status}{wip}  "{cl.subject}"'
        line2 = (
            f"  {cl.owner}  "
            f"+{cl.insertions}/-{cl.deletions}  "
            f"ps{cl.patchset if cl.patchset is not None else '?'}  "
            f"updated {cl.updated[:10]}"
            f"{labels_str}{comments}"
        )

        lines = [line1, line2]

        if cl.reviewers:
            lines.append(f"  reviewers: {', '.join(cl.reviewers)}")

        if cl.attention:
            attn = [f"{a.email} ({a.reason})" for a in cl.attention]
            lines.append(f"  attention: {', '.join(attn)}")

        blocks.append("\n".join(lines))

    header = f"{len(cls)} CL(s) found\n"
    return header + "\n\n".join(blocks)
