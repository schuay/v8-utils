"""Rich table reporting for detect and compare results."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import TYPE_CHECKING

import pandas as pd
from rich import box
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.table import Table

from .models import ChangePoint, CommitInfo

if TYPE_CHECKING:
    from .commits import CommitStore

console = Console()


# ── Shared helpers ───────────────────────────────────────────────────────────


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


# ── Detect report ────────────────────────────────────────────────────────────


def _format_candidates(cp: ChangePoint) -> str | None:
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


def _get_commit_info(
    commit_id: int,
    commit_store: CommitStore | None,
    engine: str | None,
) -> CommitInfo | None:
    if commit_store and engine:
        return commit_store.get(engine, commit_id)
    return None


def _get_commit_range(
    cp: ChangePoint,
    commit_store: CommitStore | None,
    engine: str | None,
) -> list[CommitInfo]:
    if commit_store and engine:
        result = commit_store.get_range(engine, cp.prev_commit_id, cp.commit_id)
        if result:
            return result
    return []


def print_detect_report(
    results: list[ChangePoint],
    group_by_commit: bool = False,
    commit_store: CommitStore | None = None,
    engine: str | None = None,
):
    """Print change-point results as rich tables."""
    if not results:
        console.print("No change points detected.")
        return

    if group_by_commit:
        _print_grouped(results, commit_store, engine)
    else:
        _print_flat(results, commit_store, engine)


def _print_grouped(
    results: list[ChangePoint],
    commit_store: CommitStore | None,
    engine: str | None,
):
    groups: dict[int, list[ChangePoint]] = defaultdict(list)
    for cp in results:
        groups[cp.commit_id].append(cp)

    has_commit_info = False
    for cid in sorted(groups):
        cp0 = groups[cid][0]
        range_commits = _get_commit_range(cp0, commit_store, engine)

        if len(range_commits) <= 1:
            info = _get_commit_info(cid, commit_store, engine)
            if info and info.title:
                has_commit_info = True
                header = f"Commit {_fmt_commit(info)}"
            elif info and info.hash:
                header = f"Commit {cid} {info.hash[:10]}"
            else:
                header = f"Commit {cid}"
        else:
            header = (
                f"Commit range {cp0.prev_commit_id + 1}..{cid}"
                f" ({len(range_commits)} commits)"
            )

        console.print(f"\n[bold]{header}[/bold]")

        alt = _format_candidates(cp0)
        if alt:
            console.print(f"  candidates: {alt}")
            for c_cid, c_prob in cp0.candidates:
                if c_prob < 0.05:
                    continue
                c_info = _get_commit_info(c_cid, commit_store, engine)
                if c_info:
                    console.print(f"  [dim]{_fmt_commit(c_info)}[/dim]")
        else:
            for c in range_commits:
                console.print(f"  [dim]{_fmt_commit(c)}[/dim]")

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
                rich_escape(cp.benchmark),
                cp.metric,
                f"[{color}]{pct:+.2f}%[/{color}]",
                f"{cp.cohens_d:.2f}d",
                p_str,
                cp.confidence,
            )
        console.print(table)

    if not has_commit_info:
        eng = engine or "<engine>"
        console.print(
            f"\n[yellow]Warning: no commit metadata available — "
            f"titles and authors are missing.[/yellow]"
        )
        console.print(
            f"[dim]To fix, configure the source repo and sync:\n"
            f"  1. Set {eng}_dir in ~/.config/v8-utils/config.toml\n"
            f'  2. Set engine = "{eng}" for this source in config\n'
            f"  3. Run: pd sync {eng}[/dim]"
        )


def _print_flat(
    results: list[ChangePoint],
    commit_store: CommitStore | None,
    engine: str | None,
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

        range_commits = _get_commit_range(cp, commit_store, engine)
        info = _get_commit_info(cp.commit_id, commit_store, engine)

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
            rich_escape(cp.benchmark),
            cp.metric,
            f"[{color}]{pct:+.2f}%[/{color}]",
            f"{cp.cohens_d:.2f}d",
            p_str,
            cp.confidence,
            desc,
        )
    console.print(table)


# ── Compare report ───────────────────────────────────────────────────────────


def print_compare_report(
    result_df: pd.DataFrame,
    key_cols: list[str],
    header_lines: list[str],
    show_all: bool = False,
):
    """Print AB comparison results as a rich table."""
    if result_df.empty:
        console.print("No matching data to compare.")
        return

    visible = result_df if show_all else result_df[result_df["significant"]]
    omitted = len(result_df) - len(visible)

    if visible.empty:
        for line in header_lines:
            console.print(f"[dim]{line}[/dim]")
        console.print("\n(no statistically significant results)")
        return

    for line in header_lines:
        console.print(f"[dim]{line}[/dim]")

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold", padding=(0, 1))
    for col in key_cols:
        table.add_column(col.upper())
    table.add_column("A MEAN±STD", justify="right")
    table.add_column("B MEAN±STD", justify="right")
    table.add_column("CHG%", justify="right")
    table.add_column("P", justify="right")
    table.add_column("SIG")

    for _, row in visible.iterrows():
        pct = row["pct_change"] * 100
        color = "green" if pct > 0 else "red"

        a_cell = f"{row['a_mean']:.3f} ±{row['a_stdev']:.3f}"
        b_cell = f"{row['b_mean']:.3f} ±{row['b_stdev']:.3f}"
        p_adj = row["p_adj"]
        p_cell = f"{p_adj:.4f}" if not math.isnan(p_adj) else "—"

        cols = [str(row[c]) for c in key_cols]
        table.add_row(
            *cols,
            a_cell,
            b_cell,
            f"[{color}]{pct:+.2f}%[/{color}]",
            p_cell,
            "*" if row["significant"] else "",
        )

    console.print(table)
    if omitted:
        console.print(
            f"[dim]({omitted} non-significant result{'s' if omitted != 1 else ''} omitted)[/dim]"
        )
