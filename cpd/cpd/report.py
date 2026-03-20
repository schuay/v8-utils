"""Rich table reporting for change-point results.

Matches slipstream's grouped display: commit ranges with titles,
candidate probabilities with titles, and per-group benchmark tables.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from rich import box
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.table import Table

from .models import ChangePoint, CommitInfo, SeriesPoint

if TYPE_CHECKING:
    from .commits import CommitStore

console = Console()


def _fmt_commit(c: CommitInfo) -> str:
    """Format a commit as: id hash author title."""
    parts = [str(c.id)]
    if c.hash:
        parts.append(c.hash[:10])
    if c.author:
        parts.append(c.author)
    if c.title:
        parts.append(rich_escape(c.title[:70]))
    return " ".join(parts)


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


def _get_commit_range(
    cp: ChangePoint,
    commit_store: CommitStore | None,
    engine: str | None,
    series_points: dict[int, SeriesPoint],
) -> list[CommitInfo]:
    """Get all commits between prev_commit_id and commit_id."""
    if commit_store and engine:
        result = commit_store.get_range(engine, cp.prev_commit_id, cp.commit_id)
        if result:
            return result

    # Fallback: use series point data for the changepoint commit itself
    info = series_points.get(cp.commit_id)
    if info and info.commit_hash:
        return [
            CommitInfo(
                id=cp.commit_id,
                hash=info.commit_hash or "",
                date=info.commit_date or "",
                timestamp=0,
                title=info.commit_title or "",
            )
        ]
    return []


def _get_commit_info(
    commit_id: int,
    commit_store: CommitStore | None,
    engine: str | None,
    series_points: dict[int, SeriesPoint],
) -> CommitInfo | None:
    """Look up commit info from store or series data."""
    if commit_store and engine:
        info = commit_store.get(engine, commit_id)
        if info:
            return info

    pt = series_points.get(commit_id)
    if pt and pt.commit_hash:
        return CommitInfo(
            id=commit_id,
            hash=pt.commit_hash or "",
            date=pt.commit_date or "",
            timestamp=0,
            title=pt.commit_title or "",
        )
    return None


def print_report(
    results: list[ChangePoint],
    group_by_commit: bool = False,
    series_data: dict | None = None,
    commit_store: CommitStore | None = None,
    engine: str | None = None,
):
    """Print change-point results as rich tables.

    Args:
        results: Detected change points.
        group_by_commit: If True, group results by commit_id.
        series_data: Optional {SeriesKey: list[SeriesPoint]} for fallback metadata.
        commit_store: Optional CommitStore for commit titles and ranges.
        engine: Engine name for commit store lookups.
    """
    if not results:
        console.print("No change points detected.")
        return

    # Build commit_id → SeriesPoint lookup from series data
    series_points: dict[int, SeriesPoint] = {}
    if series_data:
        for points in series_data.values():
            for p in points:
                if p.commit_id not in series_points:
                    series_points[p.commit_id] = p

    if group_by_commit:
        _print_grouped(results, commit_store, engine, series_points)
    else:
        _print_flat(results, commit_store, engine, series_points)


def _print_grouped(
    results: list[ChangePoint],
    commit_store: CommitStore | None,
    engine: str | None,
    series_points: dict[int, SeriesPoint],
):
    groups: dict[int, list[ChangePoint]] = defaultdict(list)
    for cp in results:
        groups[cp.commit_id].append(cp)

    for cid in sorted(groups):
        cp0 = groups[cid][0]
        range_commits = _get_commit_range(cp0, commit_store, engine, series_points)

        # Header
        if len(range_commits) <= 1:
            info = _get_commit_info(cid, commit_store, engine, series_points)
            if info and info.hash:
                h = info.hash[:10]
                title = rich_escape(info.title[:70]) if info.title else ""
                header = f"Commit {cid} {h} {title}".strip()
            else:
                header = f"Commit {cid}"
        else:
            header = (
                f"Commit range {cp0.prev_commit_id + 1}..{cid}"
                f" ({len(range_commits)} commits)"
            )

        console.print(f"\n[bold]{header}[/bold]")

        # Candidates or commit range listing
        alt = _format_candidates(cp0)
        if alt:
            console.print(f"  candidates: {alt}")
            for c_cid, c_prob in cp0.candidates:
                if c_prob < 0.05:
                    continue
                c_info = _get_commit_info(c_cid, commit_store, engine, series_points)
                if c_info:
                    console.print(f"  [dim]{_fmt_commit(c_info)}[/dim]")
        else:
            for c in range_commits:
                console.print(f"  [dim]{_fmt_commit(c)}[/dim]")

        # Benchmark table
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


def _print_flat(
    results: list[ChangePoint],
    commit_store: CommitStore | None,
    engine: str | None,
    series_points: dict[int, SeriesPoint],
):
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

        range_commits = _get_commit_range(cp, commit_store, engine, series_points)
        info = _get_commit_info(cp.commit_id, commit_store, engine, series_points)

        if len(range_commits) <= 1 and info and info.hash:
            desc = _fmt_commit(info)
        else:
            n = len(range_commits)
            desc = f"{cp.prev_commit_id + 1}..{cp.commit_id} ({n} commits)"

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
