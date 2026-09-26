"""CLI for pd -- perf data analysis.

Argument parsing and rendering only; the operations are v8_utils.pd.api.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Annotated, Optional

import typer

from . import api
from .adaptor import discover
from .commits import CommitStore
from .engines import ENGINES, get_id_regex, get_path_filter, get_src_dir, sync_engine
from .report import print_at_report, print_compare_report, print_detect_report

app = typer.Typer(
    help="Perf data analysis -- change-point detection, AB comparison, and more."
)


@contextlib.contextmanager
def _cli_errors():
    """A ValueError from the api is the message; exit 1 with it on stderr."""
    try:
        yield
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e


def _verbose_logging(enabled: bool) -> None:
    """--verbose: the api's timing and progress lines, on stderr."""
    if not enabled:
        return
    logger = logging.getLogger("v8_utils.pd")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)


def _parse_date(value: str) -> str:
    try:
        return api.parse_date(value)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e


# ── detect ───────────────────────────────────────────────────────────────────


@app.command()
def detect(
    source: Annotated[str, typer.Argument(help="Data source name")],
    bot: Annotated[Optional[str], typer.Option("--bot", help="Bot name filter")] = None,
    benchmark: Annotated[
        Optional[str], typer.Option("--benchmark", "-b", help="Benchmark name filter")
    ] = None,
    metric: Annotated[
        Optional[str], typer.Option("--metric", "-m", help="Metric/test glob filter")
    ] = None,
    engine_filter: Annotated[
        Optional[str],
        typer.Option(
            "--engine", help="Engine filter (e.g. v8, jsc) for sources that expose it"
        ),
    ] = None,
    since: Annotated[
        Optional[str],
        typer.Option(
            help="Only include commits after this date (YYYY-MM-DD or '2 weeks ago')"
        ),
    ] = None,
    until: Annotated[
        Optional[str],
        typer.Option(help="Only include commits before this date"),
    ] = None,
    penalty: Annotated[
        Optional[float], typer.Option("--penalty", help="PELT penalty")
    ] = None,
    min_effect: Annotated[
        Optional[float], typer.Option("--min-effect", help="Min Cohen's d")
    ] = None,
    min_change: Annotated[
        Optional[float],
        typer.Option("--min-change", help="Min percent change (e.g. 5 = 5%)"),
    ] = None,
    group_by_commit: Annotated[
        bool, typer.Option("--group", help="Group results by commit")
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show timing and progress info")
    ] = False,
):
    """Detect change points in benchmark time series."""
    _verbose_logging(verbose)
    store = CommitStore()
    try:
        with _cli_errors():
            det = api.detect(
                source,
                store=store,
                bot=bot,
                benchmark=benchmark,
                metric=metric,
                engine=engine_filter,
                since=_parse_date(since) if since else None,
                until=_parse_date(until) if until else None,
                penalty=penalty,
                min_effect=min_effect,
                min_change=min_change,
            )
        print_detect_report(
            det.points,
            group_by_commit=group_by_commit,
            commit_store=store,
            default_engine=det.default_engine,
            verbose=verbose,
        )
    finally:
        store.close()


# ── compare ──────────────────────────────────────────────────────────────────


@app.command()
def compare(
    source: Annotated[str, typer.Argument(help="Data source name")],
    a: Annotated[
        list[str],
        typer.Option(
            "--a", help="A-side overrides: field=value (e.g. variant=default)"
        ),
    ],
    b: Annotated[
        list[str],
        typer.Option(
            "--b", help="B-side overrides: field=value (e.g. variant=turbolev)"
        ),
    ],
    bot: Annotated[
        Optional[str], typer.Option("--bot", help="Bot filter (both sides)")
    ] = None,
    benchmark: Annotated[
        Optional[str],
        typer.Option("--benchmark", "-b", help="Benchmark filter (both sides)"),
    ] = None,
    since: Annotated[Optional[str], typer.Option(help="Since date")] = None,
    until: Annotated[Optional[str], typer.Option(help="Until date")] = None,
    show_all: Annotated[
        bool, typer.Option("--show-all", help="Include non-significant results")
    ] = False,
    alpha: Annotated[
        float, typer.Option("--alpha", help="Significance threshold")
    ] = 0.05,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show timing and progress info")
    ] = False,
):
    """Compare two configurations (A vs B) of benchmark data."""
    _verbose_logging(verbose)
    with _cli_errors():
        cmp = api.compare(
            source,
            a,
            b,
            bot=bot,
            benchmark=benchmark,
            since=_parse_date(since) if since else None,
            until=_parse_date(until) if until else None,
            alpha=alpha,
        )
    print_compare_report(cmp.table, cmp.key_cols, cmp.header, show_all=show_all)


# ── commit-impact ────────────────────────────────────────────────────────────


@app.command()
def commit_impact(
    source: Annotated[str, typer.Argument(help="Data source name")],
    commit: Annotated[
        str, typer.Argument(help="Target commit position (id) or git hash prefix")
    ],
    bot: Annotated[Optional[str], typer.Option("--bot", help="Bot name filter")] = None,
    benchmark: Annotated[
        Optional[str], typer.Option("--benchmark", "-b", help="Benchmark name filter")
    ] = None,
    variant: Annotated[
        Optional[str], typer.Option("--variant", help="Variant filter")
    ] = None,
    engine_filter: Annotated[
        Optional[str], typer.Option("--engine", help="Engine filter (e.g. v8, jsc)")
    ] = None,
    metric: Annotated[
        Optional[str], typer.Option("--metric", "-m", help="Metric/test glob filter")
    ] = None,
    history: Annotated[
        int, typer.Option("--history", help="Commits of history for the noise estimate")
    ] = 20,
    min_change: Annotated[
        Optional[float],
        typer.Option("--min-change", help="Min percent change (e.g. 3 = 3%)"),
    ] = None,
    min_z: Annotated[
        Optional[float], typer.Option("--min-z", help="Min |z| to flag")
    ] = None,
    since: Annotated[
        Optional[str], typer.Option(help="Fetch window start (default: derived from C)")
    ] = None,
    show_all: Annotated[
        bool, typer.Option("--show-all", help="Include below-threshold series")
    ] = False,
    chart_only: Annotated[
        bool, typer.Option("--charts", help="One sparkline line per series, no table")
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show timing and progress info")
    ] = False,
):
    """Assess what changed at a specific commit (before vs after)."""
    _verbose_logging(verbose)
    store = CommitStore()
    try:
        with _cli_errors():
            impact = api.commit_impact(
                source,
                commit,
                store=store,
                bot=bot,
                benchmark=benchmark,
                variant=variant,
                metric=metric,
                engine=engine_filter,
                history=history,
                min_change=min_change,
                min_z=min_z,
                since=_parse_date(since) if since else None,
            )
    finally:
        store.close()
    print_at_report(
        impact.deltas,
        impact.snapped_commit_id,
        impact.header,
        show_all=show_all,
        chart_only=chart_only,
    )


# ── sync ─────────────────────────────────────────────────────────────────────


@app.command()
def sync(
    engine: Annotated[str, typer.Argument(help="Engine to sync (v8, chromium, jsc)")],
    since: Annotated[
        Optional[str],
        typer.Option(help="Sync commits since this date (default: 6 months ago)"),
    ] = None,
    fetch: Annotated[
        bool,
        typer.Option(help="Fetch origin/main before reading git log"),
    ] = True,
):
    """Populate commit metadata from an engine's git repo."""
    # Validate here for a clear CLI error + exit code; sync_engine itself is
    # best-effort (it logs and returns 0) since it also runs on the detect path.
    if not get_id_regex(engine):
        typer.echo(f"Error: unknown engine '{engine}'", err=True)
        typer.echo(f"Available: {', '.join(sorted(ENGINES))}", err=True)
        raise typer.Exit(1)

    src_dir = get_src_dir(engine)
    if not src_dir or not src_dir.is_dir():
        typer.echo(
            f"Error: source directory for '{engine}' not found."
            f" Check v8-utils config (~/.config/v8-utils/config.toml).",
            err=True,
        )
        raise typer.Exit(1)

    since_date = since or "6 months ago"
    path = get_path_filter(engine)
    suffix = f" path={path}" if path else ""
    typer.echo(
        f"Syncing {engine} commits from {src_dir} (since {since_date}){suffix}..."
    )
    store = CommitStore()
    count = sync_engine(store, engine, since=since_date, fetch=fetch)
    typer.echo(f"  {count} commits processed.")
    store.close()


# ── sources ──────────────────────────────────────────────────────────────────


@app.command()
def sources():
    """List configured data sources and available adaptors."""
    src = api.load_config().sources

    if src:
        typer.echo("Configured sources:")
        for name, scfg in sorted(src.items()):
            adaptor = scfg.get("adaptor", name)
            engine = scfg.get("engine", "")
            typer.echo(f"  {name} (adaptor={adaptor}, engine={engine})")
    else:
        typer.echo("No sources configured.")

    typer.echo("\nAvailable adaptors:")
    for name in sorted(discover()):
        typer.echo(f"  {name}")
