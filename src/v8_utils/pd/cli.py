"""CLI for pd — perf data analysis."""

from __future__ import annotations

import subprocess
import time
from fnmatch import fnmatch
from typing import Annotated, Optional

import typer

from .adaptor import discover
from .commits import CommitStore
from .detect import detect_from_df
from .engines import ENGINES, get_id_regex, get_src_dir
from .models import AnalysisConfig
from .report import print_compare_report, print_detect_report

app = typer.Typer(
    help="Perf data analysis — change-point detection, AB comparison, and more."
)


def _load_config() -> dict:
    """Load sources and analysis config from v8-utils config."""
    try:
        from .. import config as v8_config

        cfg = v8_config.load()
        return {
            "sources": getattr(cfg, "sources", {}),
            "analysis": getattr(cfg, "analysis", {}),
        }
    except Exception:
        return {"sources": {}, "analysis": {}}


def _make_adaptor(source: str, cfg: dict):
    """Instantiate an adaptor for the given source name."""
    sources = cfg.get("sources", {})
    if source not in sources:
        typer.echo(f"Error: unknown source '{source}'", err=True)
        available = ", ".join(sorted(sources)) if sources else "(none configured)"
        typer.echo(f"Available: {available}", err=True)
        raise typer.Exit(1)

    source_cfg = dict(sources[source])
    adaptor_name = source_cfg.pop("adaptor", source)

    adaptors = discover()
    if adaptor_name not in adaptors:
        typer.echo(f"Error: adaptor '{adaptor_name}' not found", err=True)
        typer.echo(f"Available: {', '.join(sorted(adaptors))}", err=True)
        raise typer.Exit(1)

    return adaptors[adaptor_name](**source_cfg)


def _engine_for_source(source: str, cfg: dict) -> str | None:
    sources = cfg.get("sources", {})
    return sources.get(source, {}).get("engine")


def _parse_date(value: str) -> str:
    """Parse a date string like '2026-01-15' or '2 weeks ago' into YYYY-MM-DD."""
    import dateparser

    dt = dateparser.parse(value, settings={"PREFER_DATES_FROM": "past"})
    if dt is None:
        raise typer.BadParameter(f"Cannot parse date: {value}")
    return dt.strftime("%Y-%m-%d")


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
        Optional[float], typer.Option("--min-change", help="Min pct change")
    ] = None,
    group_by_commit: Annotated[
        bool, typer.Option("--group", help="Group results by commit")
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show timing and progress info")
    ] = False,
):
    """Detect change points in benchmark time series."""
    cfg = _load_config()

    since_date = _parse_date(since) if since else None
    until_date = _parse_date(until) if until else None

    analysis_cfg = cfg.get("analysis", {})
    config = AnalysisConfig(
        penalty=penalty or analysis_cfg.get("penalty", 3.0),
        min_effect_size=min_effect or analysis_cfg.get("min_effect_size", 0.5),
        min_pct_change=min_change or analysis_cfg.get("min_pct_change", 0.01),
    )

    filter_kwargs = {}
    if bot:
        filter_kwargs["bot"] = bot
    if benchmark:
        filter_kwargs["benchmark"] = benchmark

    adaptor = _make_adaptor(source, cfg)
    engine = _engine_for_source(source, cfg)
    commit_store = CommitStore()

    def _log(msg: str) -> None:
        if verbose:
            typer.echo(msg, err=True)

    t0 = time.monotonic()
    _log("fetching data...")
    fetched = adaptor.fetch(since=since_date, until=until_date, **filter_kwargs)
    _log(f"fetch: {len(fetched)} rows in {time.monotonic() - t0:.1f}s")

    # Apply --metric glob filter
    if metric:
        fetched = fetched[fetched["test"].apply(lambda t: fnmatch(t, metric))]
        _log(f"  after --metric filter: {len(fetched)} rows")

    t0 = time.monotonic()
    results = detect_from_df(fetched, config)
    _log(f"detect: {len(results)} change points in {time.monotonic() - t0:.1f}s")

    print_detect_report(
        results,
        group_by_commit=group_by_commit,
        commit_store=commit_store,
        engine=engine,
        verbose=verbose,
    )
    commit_store.close()


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
    from .compare import compare_snapshots

    cfg = _load_config()

    since_date = _parse_date(since) if since else None
    until_date = _parse_date(until) if until else None

    # Parse --a / --b overrides
    def _parse_overrides(items: list[str]) -> dict[str, str]:
        result = {}
        for item in items:
            if "=" not in item:
                typer.echo(f"Error: override must be key=value, got: {item}", err=True)
                raise typer.Exit(1)
            k, v = item.split("=", 1)
            result[k] = v
        return result

    a_overrides = _parse_overrides(a)
    b_overrides = _parse_overrides(b)

    # Build common filters
    common = {}
    if bot:
        common["bot"] = bot
    if benchmark:
        common["benchmark"] = benchmark

    adaptor = _make_adaptor(source, cfg)

    def _log(msg: str) -> None:
        if verbose:
            typer.echo(msg, err=True)

    # Fetch both sides
    t0 = time.monotonic()
    filters_a = {**common, **a_overrides}
    filters_b = {**common, **b_overrides}
    _log(f"fetching A: {filters_a}")
    df_a = adaptor.fetch(since=since_date, until=until_date, **filters_a)
    _log(f"fetching B: {filters_b}")
    df_b = adaptor.fetch(since=since_date, until=until_date, **filters_b)
    _log(f"fetch: {len(df_a)} + {len(df_b)} rows in {time.monotonic() - t0:.1f}s")

    # Determine key columns: all dimension columns NOT mentioned in overrides
    all_override_keys = set(a_overrides) | set(b_overrides)
    dimension_cols = ["bot", "benchmark", "test", "variant"]
    key_cols = [c for c in dimension_cols if c not in all_override_keys]

    t0 = time.monotonic()
    result_df = compare_snapshots(df_a, df_b, key_cols, alpha=alpha)
    _log(f"compare: {len(result_df)} rows in {time.monotonic() - t0:.1f}s")

    # Build header
    a_desc = " ".join(f"{k}={v}" for k, v in a_overrides.items())
    b_desc = " ".join(f"{k}={v}" for k, v in b_overrides.items())
    common_desc = " ".join(f"{k}={v}" for k, v in common.items())
    header = [f"A: {a_desc}  B: {b_desc}"]
    if common_desc:
        header.append(f"common: {common_desc}")

    print_compare_report(result_df, key_cols, header, show_all=show_all)


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
    id_regex = get_id_regex(engine)
    if not id_regex:
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

    if fetch:
        typer.echo(f"Fetching origin/main in {src_dir}...")
        res = subprocess.run(
            "git fetch origin main",
            shell=True,
            cwd=src_dir,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            typer.echo(f"  fetch failed: {res.stderr.strip()}", err=True)

    since_date = since or "6 months ago"

    store = CommitStore()
    typer.echo(f"Syncing {engine} commits from {src_dir} (since {since_date})...")
    count = store.populate(engine, src_dir, id_regex, since=since_date)
    typer.echo(f"  {count} commits processed.")
    store.close()


# ── sources ──────────────────────────────────────────────────────────────────


@app.command()
def sources():
    """List configured data sources and available adaptors."""
    cfg = _load_config()
    src = cfg.get("sources", {})

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
