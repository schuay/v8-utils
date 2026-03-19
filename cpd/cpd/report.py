"""Rich table reporting for change-point results."""

from __future__ import annotations

from collections import defaultdict

from rich import box
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.table import Table

from .models import ChangePoint, SeriesPoint

console = Console()


def _format_candidates(cp: ChangePoint) -> str | None:
    """Format alternative breakpoint candidates, or None if unambiguous."""
    if not cp.candidates:
        return None
    top_prob = max(p for _, p in cp.candidates)
    if top_prob >= 0.90:
        return None
    parts = []
    for cid, prob in cp.candidates:
        if prob >= 0.05:
            parts.append(f"{cid}[dim]({prob:.0%})[/dim]")
    return " | ".join(parts) if parts else None


def print_report(
    results: list[ChangePoint],
    group_by_commit: bool = False,
    series_data: dict | None = None,
):
    """Print change-point results as rich tables.

    Args:
        results: Detected change points.
        group_by_commit: If True, group results by commit_id.
        series_data: Optional {SeriesKey: list[SeriesPoint]} for commit metadata.
    """
    if not results:
        console.print("No change points detected.")
        return

    # Collect commit metadata from series data if available
    commit_info: dict[int, SeriesPoint] = {}
    if series_data:
        for points in series_data.values():
            for p in points:
                if p.commit_id not in commit_info:
                    commit_info[p.commit_id] = p

    if group_by_commit:
        _print_grouped(results, commit_info)
    else:
        _print_flat(results, commit_info)


def _commit_desc(cp: ChangePoint, commit_info: dict[int, SeriesPoint]) -> str:
    info = commit_info.get(cp.commit_id)
    if info and info.commit_hash:
        h = info.commit_hash[:10]
        title = rich_escape(info.commit_title[:40]) if info.commit_title else ""
        return f"{cp.commit_id} {h} {title}".strip()
    return f"{cp.prev_commit_id + 1}..{cp.commit_id}"


def _print_grouped(results: list[ChangePoint], commit_info: dict[int, SeriesPoint]):
    groups: dict[int, list[ChangePoint]] = defaultdict(list)
    for cp in results:
        groups[cp.commit_id].append(cp)

    for cid in sorted(groups):
        info = commit_info.get(cid)
        if info and info.commit_hash:
            h = info.commit_hash[:10]
            title = rich_escape(info.commit_title[:70]) if info.commit_title else ""
            header = f"Commit {cid} {h} {title}".strip()
        else:
            prev_cid = groups[cid][0].prev_commit_id
            header = f"Commit range {prev_cid + 1}..{cid}"

        console.print(f"\n[bold]{header}[/bold]")
        alt = _format_candidates(groups[cid][0])
        if alt:
            console.print(f"  candidates: {alt}")

        table = Table(
            box=box.SIMPLE, show_header=True, header_style="bold", padding=(0, 1)
        )
        table.add_column("BENCHMARK")
        table.add_column("METRIC")
        table.add_column("CHANGE", justify="right")
        table.add_column("EFFECT", justify="right")
        table.add_column("P-VALUE", justify="right")
        table.add_column("CONF")

        for cp in sorted(groups[cid], key=lambda x: abs(x.pct_change), reverse=True):
            pct = cp.pct_change * 100
            color = "green" if cp.direction == "improvement" else "red"
            p_str = f"{cp.p_value:.1e}" if cp.p_value < 0.01 else f"{cp.p_value:.3f}"
            table.add_row(
                rich_escape(cp.series.benchmark),
                cp.series.metric,
                f"[{color}]{pct:+.2f}%[/{color}]",
                f"{cp.cohens_d:.2f}d",
                p_str,
                cp.confidence,
            )
        console.print(table)


def _print_flat(results: list[ChangePoint], commit_info: dict[int, SeriesPoint]):
    results = sorted(results, key=lambda x: abs(x.pct_change), reverse=True)

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold", padding=(0, 1))
    table.add_column("BENCHMARK")
    table.add_column("METRIC")
    table.add_column("CHANGE", justify="right")
    table.add_column("EFFECT", justify="right")
    table.add_column("P-VALUE", justify="right")
    table.add_column("CONF")
    table.add_column("COMMIT", no_wrap=False)

    for cp in results:
        pct = cp.pct_change * 100
        color = "green" if cp.direction == "improvement" else "red"
        desc = _commit_desc(cp, commit_info)
        alt = _format_candidates(cp)
        if alt:
            desc += f"\n  also: {alt}"
        p_str = f"{cp.p_value:.1e}" if cp.p_value < 0.01 else f"{cp.p_value:.3f}"
        table.add_row(
            rich_escape(cp.series.benchmark),
            cp.series.metric,
            f"[{color}]{pct:+.2f}%[/{color}]",
            f"{cp.cohens_d:.2f}d",
            p_str,
            cp.confidence,
            desc,
        )
    console.print(table)
