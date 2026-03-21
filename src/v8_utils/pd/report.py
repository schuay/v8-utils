"""Rich table reporting for detect and compare results."""

from __future__ import annotations

import math
import re
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

_V8_ROLL_RE = re.compile(r"Roll V8 from ([0-9a-f]{10,}) to ([0-9a-f]{10,})")
_MAX_COMMITS_SHOWN = 10


# ── Shared helpers ───────────────────────────────────────────────────────────


def _short_author(author: str) -> str:
    """Shorten google.com/chromium.org authors to name@, show full email otherwise."""
    if not author:
        return ""
    # If it looks like an email
    if "@" in author:
        name, _, domain = author.partition("@")
        if domain in ("google.com", "chromium.org"):
            return f"{name}@"
    return author


def _fmt_commit(c: CommitInfo) -> str:
    """Format a commit as: id hash author title."""
    parts = [str(c.id)]
    if c.hash:
        parts.append(c.hash[:10])
    if c.author:
        parts.append(_short_author(c.author))
    if c.title:
        parts.append(rich_escape(c.title[:70]))
    return " ".join(parts)


def _fmt_range_header(prev_id: int, cid: int, n_commits: int) -> str:
    """Format a commit range header like '123..456 (5 commits)' or just '123'."""
    first = prev_id + 1
    if first == cid:
        return str(cid)
    return f"{first}..{cid} ({n_commits} commit{'s' if n_commits != 1 else ''})"


def _print_commit_range(
    commits: list[CommitInfo],
    commit_store: CommitStore | None,
    indent: str = "  ",
    verbose: bool = False,
):
    """Print a list of commits, surfacing V8 rolls first, then capping the rest."""
    if verbose:
        titles_with_roll = [
            c for c in commits if c.title and _V8_ROLL_RE.search(c.title)
        ]
        console.print(
            f"{indent}[dim yellow]debug: {len(commits)} commits in range, "
            f"{len(titles_with_roll)} V8 rolls detected, "
            f"commit_store={'yes' if commit_store else 'no'}[/dim yellow]",
            highlight=False,
        )
        if commits:
            c0 = commits[0]
            console.print(
                f"{indent}[dim yellow]debug: first commit: id={c0.id} "
                f"title={c0.title[:60] if c0.title else '(empty)'}[/dim yellow]",
                highlight=False,
            )

    # Separate V8 rolls from other commits
    rolls: list[tuple[CommitInfo, list[CommitInfo]]] = []
    others: list[CommitInfo] = []

    for c in commits:
        if commit_store and c.title:
            m = _V8_ROLL_RE.search(c.title)
            if m:
                v8_commits = _resolve_v8_roll(
                    commit_store, m.group(1), m.group(2), verbose=verbose
                )
                rolls.append((c, v8_commits))
                continue
        others.append(c)

    # Always show V8 rolls with their expansions
    for c, v8_commits in rolls:
        console.print(f"{indent}[dim]{_fmt_commit(c)}[/dim]")
        if v8_commits:
            for vc in v8_commits:
                console.print(f"{indent}  [dim]{_fmt_commit(vc)}[/dim]")

    # Show remaining commits, capped
    for i, c in enumerate(others):
        if i >= _MAX_COMMITS_SHOWN:
            remaining = len(others) - i
            console.print(f"{indent}[dim]... and {remaining} more[/dim]")
            break
        console.print(f"{indent}[dim]{_fmt_commit(c)}[/dim]")


def _resolve_v8_roll(
    commit_store: CommitStore,
    from_hash: str,
    to_hash: str,
    verbose: bool = False,
) -> list[CommitInfo]:
    """Look up V8 commits between two hashes via the commit store."""
    from_info = commit_store.get_by_hash("v8", from_hash)
    to_info = commit_store.get_by_hash("v8", to_hash)
    if verbose:
        console.print(
            f"    [dim yellow]debug: v8 roll {from_hash}..{to_hash} → "
            f"from={'#' + str(from_info.id) if from_info else 'miss'} "
            f"to={'#' + str(to_info.id) if to_info else 'miss'}[/dim yellow]",
            highlight=False,
        )
    if not from_info or not to_info:
        return []
    result = commit_store.get_range("v8", from_info.id, to_info.id)
    if verbose:
        console.print(
            f"    [dim yellow]debug: v8 range {from_info.id}..{to_info.id} → {len(result)} commits[/dim yellow]",
            highlight=False,
        )
    return result


# ── Detect report ────────────────────────────────────────────────────────────


def _format_candidates(
    cp: ChangePoint,
    commit_store: CommitStore | None,
    engine: str | None,
) -> str | None:
    """Format candidate breakpoints as ranges with probabilities."""
    if not cp.candidates:
        return None
    top_prob = max(p for _, p in cp.candidates)
    if top_prob >= 0.90:
        return None

    parts = []
    prev_cid = cp.prev_commit_id
    for cid, prob in cp.candidates:
        if prob < 0.05:
            continue
        # Each candidate is a range from prev data point to this one
        n = 0
        if commit_store and engine:
            range_commits = commit_store.get_range(engine, prev_cid, cid)
            n = len(range_commits)

        if n > 1:
            label = f"{prev_cid + 1}..{cid}"
        else:
            label = str(cid)
        parts.append(f"{label}[dim]({prob:.0%})[/dim]")

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
    verbose: bool = False,
):
    """Print change-point results as rich tables."""
    if not results:
        console.print("No change points detected.")
        return

    if verbose and commit_store and engine:
        # Check what's in the commit store for debugging
        sample = commit_store.get(engine, results[0].commit_id)
        v8_count = commit_store.conn.execute(
            "SELECT count(*) FROM commits WHERE engine='v8'"
        ).fetchone()[0]
        eng_count = commit_store.conn.execute(
            "SELECT count(*) FROM commits WHERE engine=?", (engine,)
        ).fetchone()[0]
        console.print(
            f"[dim]commit store: engine={engine}, {eng_count} {engine} commits, "
            f"{v8_count} v8 commits, "
            f"sample lookup({results[0].commit_id})={'found' if sample else 'miss'}[/dim]",
            highlight=False,
        )

    if group_by_commit:
        _print_grouped(results, commit_store, engine, verbose=verbose)
    else:
        _print_flat(results, commit_store, engine)


def _print_grouped(
    results: list[ChangePoint],
    commit_store: CommitStore | None,
    engine: str | None,
    verbose: bool = False,
):
    groups: dict[int, list[ChangePoint]] = defaultdict(list)
    for cp in results:
        groups[cp.commit_id].append(cp)

    # Only show these columns if they have more than one distinct value.
    show_variant = len({cp.variant for cp in results}) > 1
    show_submetric = any(cp.submetric for cp in results)

    has_commit_info = False
    for cid in sorted(groups):
        cp0 = groups[cid][0]
        range_commits = _get_commit_range(cp0, commit_store, engine)
        n = len(range_commits)

        # Header: always show as range since chromium data points span multiple commits
        header = _fmt_range_header(cp0.prev_commit_id, cid, n)
        if n == 1 and range_commits[0].title:
            has_commit_info = True
            header = _fmt_commit(range_commits[0])
        elif n > 1:
            has_commit_info = True
        elif n == 0:
            # No commit data — just show the ID
            info = _get_commit_info(cid, commit_store, engine)
            if info and info.title:
                has_commit_info = True
                header = _fmt_commit(info)

        console.print(f"\n[bold]{header}[/bold]")

        # Show candidates or commit range listing
        alt = _format_candidates(cp0, commit_store, engine)
        if alt:
            console.print(f"  [dim]candidates: {alt}[/dim]")
            # Show details for each candidate with significant probability
            for c_cid, c_prob in cp0.candidates:
                if c_prob < 0.05:
                    continue
                c_range = (
                    commit_store.get_range(engine, cp0.prev_commit_id, c_cid)
                    if commit_store and engine
                    else []
                )
                if c_range:
                    _print_commit_range(
                        c_range, commit_store, indent="    ", verbose=verbose
                    )
        elif range_commits:
            _print_commit_range(range_commits, commit_store, verbose=verbose)

        # Benchmark table
        table = Table(
            box=box.SIMPLE, show_header=True, header_style="bold", padding=(0, 1)
        )
        table.add_column("BENCHMARK")
        table.add_column("METRIC")
        if show_variant:
            table.add_column("VARIANT")
        if show_submetric:
            table.add_column("SUBMETRIC")
        table.add_column("CHANGE", justify="right")
        table.add_column("EFFECT", justify="right")
        table.add_column("P-VALUE", justify="right")
        table.add_column("CONF")

        for cp in sorted(groups[cid], key=lambda x: abs(x.pct_change), reverse=True):
            pct = cp.pct_change * 100
            color = "green" if cp.direction == "improvement" else "red"
            p_str = f"{cp.p_value:.1e}" if cp.p_value < 0.01 else f"{cp.p_value:.3f}"
            row_cells = [
                rich_escape(cp.benchmark),
                cp.metric,
            ]
            if show_variant:
                row_cells.append(cp.variant)
            if show_submetric:
                row_cells.append(cp.submetric)
            row_cells.extend(
                [
                    f"[{color}]{pct:+.2f}%[/{color}]",
                    f"{cp.cohens_d:.2f}d",
                    p_str,
                    cp.confidence,
                ]
            )
            table.add_row(*row_cells)
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

    show_variant = len({cp.variant for cp in results}) > 1
    show_submetric = any(cp.submetric for cp in results)

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold", padding=(0, 1))
    table.add_column("BENCHMARK")
    table.add_column("METRIC")
    if show_variant:
        table.add_column("VARIANT")
    if show_submetric:
        table.add_column("SUBMETRIC")
    table.add_column("CHANGE", justify="right")
    table.add_column("EFFECT", justify="right")
    table.add_column("P-VALUE", justify="right")
    table.add_column("CONF")
    table.add_column("COMMIT", no_wrap=False)

    for cp in results:
        pct = cp.pct_change * 100
        color = "green" if cp.direction == "improvement" else "red"

        range_commits = _get_commit_range(cp, commit_store, engine)
        n = len(range_commits)

        if n == 1 and range_commits[0].title:
            desc = _fmt_commit(range_commits[0])
        elif n > 0:
            desc = _fmt_range_header(cp.prev_commit_id, cp.commit_id, n)
        else:
            desc = str(cp.commit_id)

        alt = _format_candidates(cp, commit_store, engine)
        if alt:
            desc += f"\n  also: {alt}"

        p_str = f"{cp.p_value:.1e}" if cp.p_value < 0.01 else f"{cp.p_value:.3f}"
        row_cells = [
            rich_escape(cp.benchmark),
            cp.metric,
        ]
        if show_variant:
            row_cells.append(cp.variant)
        if show_submetric:
            row_cells.append(cp.submetric)
        row_cells.extend(
            [
                f"[{color}]{pct:+.2f}%[/{color}]",
                f"{cp.cohens_d:.2f}d",
                p_str,
                cp.confidence,
                desc,
            ]
        )
        table.add_row(*row_cells)
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
